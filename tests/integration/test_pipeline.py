"""The ingestion pipeline end to end, with GitHub and OpenAI faked out.

Everything here is the real code path -- the real diff, the real chunking, the
real SQL, the real transaction boundaries. Only the two things that leave the
machine are replaced: GitHub by an ``httpx.MockTransport``, and the embeddings
call by a function that returns vectors of the right shape.

That is the line worth drawing. Faking the database too would leave nothing being
tested except that the functions call each other; keeping it and faking the network
means a test that fails for the same reasons production would.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from portfolio_ai.config import get_settings
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import PurgeSafetyError
from portfolio_ai.ingestion import github, pipeline
from portfolio_ai.llm.embeddings import EmbeddingResult

pytestmark = pytest.mark.integration

REPO = "mmihaylov94/my-portfolio"

ARTICLE = """---
doc_id: {doc_id}
title: {title}
page_type: about
url: https://mihaylov.io/{doc_id}
source_type: knowledgebase
tags: [one, two]
last_verified: 2026-01-01
---

## First question?

First answer.

## Second question?

Second answer.
"""


class FakeRepo:
    """A stand-in for the knowledge base repository.

    Holds path -> contents and derives a blob SHA from the contents, exactly as
    git does in spirit: edit a file and its SHA changes, which is the entire basis
    of the fetch decision being tested.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.tree_requests = 0
        self.fetched: list[str] = []

    def add(self, doc_id: str, title: str | None = None, suffix: str = "") -> None:
        path = f"knowledgebase/{doc_id}.md"
        self.files[path] = ARTICLE.format(doc_id=doc_id, title=title or doc_id.title()) + suffix

    def blob_sha(self, path: str) -> str:
        return pipeline.content_hash(self.files[path])[:40]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            self.tree_requests += 1
            return httpx.Response(
                200,
                json={
                    "truncated": False,
                    "tree": [
                        {"type": "blob", "path": path, "sha": self.blob_sha(path)}
                        for path in sorted(self.files)
                    ]
                    # A directory and a non-Markdown file, both of which must be
                    # filtered out rather than fetched.
                    + [
                        {"type": "tree", "path": "knowledgebase/projects", "sha": "d" * 40},
                        {"type": "blob", "path": "knowledgebase/logo.png", "sha": "e" * 40},
                        {"type": "blob", "path": "README.md", "sha": "f" * 40},
                    ],
                },
            )

        path = request.url.path.split(f"/{REPO}/main/", 1)[-1]
        self.fetched.append(path)
        return httpx.Response(200, text=self.files[path])


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    """A fake repository, wired in where the real HTTP client would be built."""
    fake = FakeRepo()

    def build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(github, "build_client", build_client)
    return fake


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Replace the embeddings call. Returns a counter of how many texts it saw.

    Counting is the point: "did this run pay to embed anything" is the question
    the idempotency test asks, and it is not answerable from the database.
    """
    seen = {"texts": 0, "calls": 0}

    # Async with nothing to await, which ruff flags -- correctly in general, and
    # not here: it stands in for an async function and the caller awaits it.
    async def fake_embed(texts: list[str]) -> EmbeddingResult:  # ruff: ignore[unused-async]
        seen["texts"] += len(texts)
        seen["calls"] += 1
        return EmbeddingResult(
            vectors=[[0.01] * get_settings().embedding_dimensions for _ in texts],
            model="fake-embedding-model",
            total_tokens=len(texts) * 10,
            cost=Decimal("0.0001"),
            duration_ms=1,
        )

    monkeypatch.setattr(pipeline, "embed_texts", fake_embed)
    return lambda: seen["texts"]


@pytest.fixture(autouse=True)
async def _clean_tables() -> None:
    """Start each test from an empty knowledge base."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from documents")


async def test_a_first_run_indexes_everything(repo: FakeRepo, embedder: Callable[[], int]) -> None:
    repo.add("alpha")
    repo.add("beta")

    report = await pipeline.run()

    assert report.discovered == 2
    assert report.indexed == 2
    assert report.chunks_written == 4  # two H2 sections each
    assert report.unchanged == 0
    assert embedder() == 4

    index = await docs_db.load_index()
    assert sorted(index.by_doc_id) == ["alpha", "beta"]


@pytest.mark.usefixtures("embedder")
async def test_non_markdown_and_directories_are_filtered_out(repo: FakeRepo) -> None:
    repo.add("alpha")

    report = await pipeline.run()

    assert report.discovered == 1
    assert repo.fetched == ["knowledgebase/alpha.md"]


async def test_a_second_run_changes_nothing_and_spends_nothing(
    repo: FakeRepo, embedder: Callable[[], int]
) -> None:
    """Idempotency, and the part that matters is `embedder() == 0` on the second
    pass. A pipeline that re-embeds an unchanged corpus every night is correct and
    wasteful, and the bill is the only thing that would ever say so."""
    repo.add("alpha")
    repo.add("beta")

    await pipeline.run()
    first_pass = embedder()

    report = await pipeline.run()

    assert embedder() == first_pass, "nothing should have been re-embedded"
    assert report.indexed == 0
    assert report.unchanged == 2
    # Not downloaded either: the blob SHA matched, so the files were never asked for.
    assert repo.fetched == ["knowledgebase/alpha.md", "knowledgebase/beta.md"]


async def test_an_edited_document_is_re_indexed_and_the_others_are_not(
    repo: FakeRepo, embedder: Callable[[], int]
) -> None:
    repo.add("alpha")
    repo.add("beta")
    await pipeline.run()
    before = embedder()

    repo.add("alpha", suffix="\n## Third question?\n\nThird answer.\n")
    report = await pipeline.run()

    assert report.indexed == 1
    assert report.unchanged == 1
    assert embedder() - before == 3, "only the edited document's chunks"

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "select count(*) as n from chunks c join documents d on d.id = c.document_id "
            "where d.doc_id = 'alpha'"
        )
        row = await cur.fetchone()

    assert row is not None
    assert row["n"] == 3


async def test_force_re_embeds_everything(repo: FakeRepo, embedder: Callable[[], int]) -> None:
    """What you run after changing chunking, which the content hash cannot see."""
    repo.add("alpha")
    await pipeline.run()
    before = embedder()

    report = await pipeline.run(force=True)

    assert report.indexed == 1
    assert embedder() > before


@pytest.mark.usefixtures("embedder")
async def test_a_deleted_file_is_purged(repo: FakeRepo) -> None:
    repo.add("alpha")
    repo.add("beta")
    await pipeline.run()

    del repo.files["knowledgebase/beta.md"]
    report = await pipeline.run()

    assert report.purged == ["beta"]
    index = await docs_db.load_index()
    assert list(index.by_doc_id) == ["alpha"]


@pytest.mark.usefixtures("embedder")
async def test_discovery_collapsing_aborts_before_anything_is_written(
    repo: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario the guard exists for: GitHub has a bad minute and returns two
    documents instead of eleven. Without the guard, the other nine are purged."""
    for doc_id in ("alpha", "beta", "gamma", "delta"):
        repo.add(doc_id)
    await pipeline.run()

    del repo.files["knowledgebase/beta.md"]
    del repo.files["knowledgebase/gamma.md"]
    del repo.files["knowledgebase/delta.md"]
    monkeypatch.setattr(get_settings(), "ingestion_purge_grace", 1)

    with pytest.raises(PurgeSafetyError) as caught:
        await pipeline.run()

    assert caught.value.deletions == 3
    assert caught.value.limit == 2  # max(grace=1, ceil(4 * 0.3)=2)
    # All four survive: the early check runs before fetching or embedding, so the
    # run costs nothing and changes nothing.
    index = await docs_db.load_index()
    assert len(index) == 4


@pytest.mark.usefixtures("embedder")
async def test_a_broken_document_is_rejected_and_the_rest_still_index(repo: FakeRepo) -> None:
    repo.add("alpha")
    repo.files["knowledgebase/broken.md"] = "## No frontmatter\n\nbody"

    report = await pipeline.run()

    assert report.indexed == 1
    assert len(report.rejected) == 1
    assert report.rejected[0].path == "knowledgebase/broken.md"

    index = await docs_db.load_index()
    assert list(index.by_doc_id) == ["alpha"]


@pytest.mark.usefixtures("embedder")
async def test_a_rejected_document_does_not_get_purged_by_accident(repo: FakeRepo) -> None:
    """A document that indexed cleanly last week and is malformed this week must
    keep its last good version.

    The file still exists upstream -- we simply could not read it this run -- so
    treating it as deleted would turn one typo in one frontmatter block into that
    article disappearing from the assistant's knowledge, reported nowhere except
    as a log line naming a different thing.

    The purge guard does not help here: one rejection out of two documents is a
    single deletion, well under any sensible limit. This has to be handled by
    counting the file as seen, and nothing else would catch it.
    """
    repo.add("alpha")
    repo.add("beta")
    await pipeline.run()

    repo.files["knowledgebase/beta.md"] = "broken, no frontmatter at all"
    report = await pipeline.run()

    assert len(report.rejected) == 1
    assert report.purged == [], "the broken article must not be deleted"

    # And it is still there, still answering, from the version indexed before the
    # edit that broke it.
    index = await docs_db.load_index()
    assert sorted(index.by_doc_id) == ["alpha", "beta"]

    # The report says so explicitly, which is the other half: a rejection whose
    # previous version survived is a stale article, and one that was never indexed
    # is a missing one.
    assert report.rejected[0].retained_doc_id == "beta"
    assert "previous version retained" in report.rejected[0].describe()


@pytest.mark.usefixtures("embedder")
async def test_a_never_indexed_broken_file_reports_that_it_is_absent(repo: FakeRepo) -> None:
    """The other half of the pair. Nothing to retain, so the report should not
    imply an article is still serving when none ever was."""
    repo.add("alpha")
    repo.files["knowledgebase/new-and-broken.md"] = "no frontmatter"

    report = await pipeline.run()

    assert report.rejected[0].retained_doc_id is None
    assert "not indexed" in report.rejected[0].describe()


@pytest.mark.usefixtures("embedder")
async def test_a_retained_document_keeps_its_old_indexed_at(repo: FakeRepo) -> None:
    """Retained, but deliberately not touched.

    Advancing indexed_at would make a broken article look freshly confirmed. Left
    alone, anything watching for staleness can still see it drifting.
    """
    repo.add("alpha")
    repo.add("beta")
    await pipeline.run()

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select indexed_at from documents where doc_id = 'beta'")
        row = await cur.fetchone()
        assert row is not None
        before = row["indexed_at"]

    repo.files["knowledgebase/beta.md"] = "broken"
    await pipeline.run()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select indexed_at from documents where doc_id = 'beta'")
        row = await cur.fetchone()

    assert row is not None
    assert row["indexed_at"] == before


async def test_dry_run_writes_nothing_and_embeds_nothing(
    repo: FakeRepo, embedder: Callable[[], int]
) -> None:
    repo.add("alpha")
    repo.add("beta")

    report = await pipeline.run(dry_run=True)

    assert report.dry_run is True
    assert report.indexed == 2
    assert report.chunks_written == 4
    assert report.total_tokens > 0, "an estimate, so it should still be reported"
    assert embedder() == 0

    index = await docs_db.load_index()
    assert len(index) == 0


@pytest.mark.usefixtures("embedder")
async def test_only_restricts_the_run_and_skips_the_purge(repo: FakeRepo) -> None:
    """--only deliberately skips documents, so the set seen is not the set that
    exists. Purging against it would delete everything that was skipped."""
    repo.add("alpha")
    repo.add("beta")

    report = await pipeline.run(only=["alpha"])

    assert report.indexed == 1
    assert report.purged == []

    index = await docs_db.load_index()
    assert list(index.by_doc_id) == ["alpha"]


@pytest.mark.usefixtures("embedder")
async def test_a_renamed_file_updates_its_recorded_path(repo: FakeRepo) -> None:
    """Identical content at a new path. Re-embedding is unnecessary but leaving
    source_path stale would break the next run's fetch decision."""
    repo.add("alpha")
    await pipeline.run()

    body = repo.files.pop("knowledgebase/alpha.md")
    repo.files["knowledgebase/moved/alpha.md"] = body

    await pipeline.run()

    index = await docs_db.load_index()
    assert index.by_doc_id["alpha"].source_path == "knowledgebase/moved/alpha.md"


@pytest.mark.usefixtures("embedder")
async def test_indexed_at_advances_for_unchanged_documents(repo: FakeRepo) -> None:
    """Without the touch step an unedited document keeps its old timestamp."""
    repo.add("alpha")
    await pipeline.run()

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select indexed_at from documents where doc_id = 'alpha'")
        row = await cur.fetchone()
        assert row is not None
        first = row["indexed_at"]

    await pipeline.run()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select indexed_at from documents where doc_id = 'alpha'")
        row = await cur.fetchone()

    assert row is not None
    assert row["indexed_at"] > first
    assert isinstance(first, dt.datetime)


@pytest.mark.usefixtures("embedder")
async def test_two_files_claiming_one_doc_id_rejects_the_later_file(repo: FakeRepo) -> None:
    """doc_id is the identity everything upserts against.

    Without this check both files are fetched, chunked and paid to embed, the
    second overwrites the first's chunks, and the run reports indexing two
    documents while storing one -- with no error anywhere and one article simply
    absent from the index.

    The validator in the portfolio repository catches this at commit time. This is
    the same check at the point of damage, because a CI gate can be bypassed and
    the cost of being wrong here is silent data loss.
    """
    repo.add("alpha")
    repo.files["knowledgebase/zz-duplicate.md"] = repo.files["knowledgebase/alpha.md"].replace(
        "## First question?", "## Only in the duplicate?"
    )

    report = await pipeline.run()

    assert report.indexed == 1, "the first file by path wins"
    assert len(report.rejected) == 1
    assert report.rejected[0].path == "knowledgebase/zz-duplicate.md"
    assert "already used by knowledgebase/alpha.md" in report.rejected[0].reason

    # And the winner's content is what is stored, not the loser's.
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select section_title from chunks order by chunk_index")
        titles = [row["section_title"] for row in await cur.fetchall()]

    assert titles == ["First question?", "Second question?"]


@pytest.mark.usefixtures("embedder")
async def test_a_duplicate_cannot_take_over_an_unchanged_article(repo: FakeRepo) -> None:
    """The case the check would miss if it only looked at parsed files.

    An unchanged article is never downloaded or parsed, so its doc_id would not be
    claimed -- and a new file could quietly assume the identity of an article
    nobody had touched.
    """
    repo.add("alpha")
    await pipeline.run()

    repo.files["knowledgebase/zz-thief.md"] = repo.files["knowledgebase/alpha.md"].replace(
        "## First question?", "## Stolen?"
    )
    report = await pipeline.run()

    assert report.unchanged == 1
    assert len(report.rejected) == 1
    assert "already used by knowledgebase/alpha.md" in report.rejected[0].reason

    index = await docs_db.load_index()
    assert index.by_doc_id["alpha"].source_path == "knowledgebase/alpha.md"
