"""Storing documents and chunks, against a real Postgres.

These run the actual SQL. A unit test with a fake connection would prove the
strings are the strings we wrote; only the database can say whether the upsert
conflicts on the right column, whether the cascade fires, and whether a vector of
the wrong width is refused.
"""

import datetime as dt

import pytest
from psycopg import DataError

from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import PurgeSafetyError
from portfolio_ai.ingestion.chunking import Chunk
from portfolio_ai.ingestion.frontmatter import DocumentMeta

pytestmark = pytest.mark.integration

DIMENSIONS = 1536


def _meta(doc_id: str = "test-doc", title: str = "Test Document") -> DocumentMeta:
    return DocumentMeta(
        doc_id=doc_id,
        title=title,
        page_type="about",
        url=f"https://mihaylov.io/{doc_id}",
        tags=["one", "two"],
        last_verified=dt.date(2026, 1, 1),
    )


def _write(
    doc_id: str = "test-doc", *, path: str | None = None, digest: str = "hash-a"
) -> docs_db.DocumentWrite:
    return docs_db.DocumentWrite(
        meta=_meta(doc_id),
        source_path=path or f"knowledgebase/{doc_id}.md",
        git_blob_sha=f"blob-{doc_id}",
        content_hash=digest,
    )


def _chunks(count: int = 2) -> list[Chunk]:
    return [
        Chunk(index=i, section=f"section-{i}", section_title=f"Section {i}", content=f"Body {i}")
        for i in range(count)
    ]


def _vectors(count: int, value: float = 0.1) -> list[list[float]]:
    return [[value] * DIMENSIONS for _ in range(count)]


async def _store(write: docs_db.DocumentWrite, chunks: list[Chunk]) -> int:
    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        document_id = await docs_db.upsert_document(conn, write, dt.datetime.now(dt.UTC))
        await docs_db.replace_chunks(conn, document_id, chunks, _vectors(len(chunks)), "test-model")
    return document_id


@pytest.fixture(autouse=True)
async def _clean_tables() -> None:
    """Start every test from an empty knowledge base.

    The schema is shared across the whole session -- migrating it is expensive --
    so isolation is per test rather than per schema. Deleting documents takes the
    chunks with it through the cascade.

    Cleaning before rather than after: a failed test then leaves its rows in place
    to be looked at, and the next test still starts clean either way.
    """
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from documents")


async def test_a_document_and_its_chunks_are_stored() -> None:
    document_id = await _store(_write(), _chunks(3))

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select count(*) as n from chunks where document_id = %s", (document_id,))
        row = await cur.fetchone()

    assert row is not None
    assert row["n"] == 3


async def test_reingesting_updates_in_place_and_keeps_the_primary_key() -> None:
    """The reason for `on conflict do update` rather than delete-then-insert.

    n8n deletes and re-inserts, so a document gets a new identity every week.
    Nothing references documents yet except chunks -- which is precisely why this
    has to hold before anything else does.
    """
    first = await _store(_write(), _chunks(2))
    second = await _store(_write(digest="hash-b"), _chunks(4))

    assert first == second

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select count(*) as n from documents")
        documents = await cur.fetchone()
        await cur.execute("select count(*) as n from chunks")
        chunks = await cur.fetchone()

    assert documents is not None
    assert documents["n"] == 1
    assert chunks is not None
    # Four, not six: the old chunks were replaced rather than added to.
    assert chunks["n"] == 4


async def test_metadata_round_trips_with_its_types_intact() -> None:
    """tags is a text[] and last_verified is a date. Both arrive from YAML as real
    Python types and should come back as real Python types."""
    await _store(_write(), _chunks(1))

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select tags, last_verified, source_file from documents")
        row = await cur.fetchone()

    assert row is not None
    assert row["tags"] == ["one", "two"]
    assert row["last_verified"] == dt.date(2026, 1, 1)
    assert row["source_file"] == "test-doc.md"


async def test_load_index_reads_both_ways_round() -> None:
    await _store(_write("alpha"), _chunks(1))
    await _store(_write("beta"), _chunks(1))

    index = await docs_db.load_index()

    assert len(index) == 2
    assert index.by_doc_id["alpha"].git_blob_sha == "blob-alpha"
    # by_path is what the diff uses, before any file has been read and while
    # doc_id is therefore still unknown.
    assert index.by_path["knowledgebase/beta.md"].doc_id == "beta"


async def test_purge_removes_documents_that_were_not_seen() -> None:
    await _store(_write("kept"), _chunks(1))
    await _store(_write("gone"), _chunks(1))

    removed = await docs_db.purge_stale(["kept"], max_deletions=1)

    assert removed == ["gone"]

    index = await docs_db.load_index()
    assert list(index.by_doc_id) == ["kept"]


async def test_purge_takes_the_chunks_with_it() -> None:
    """Through the cascade in migration 0001, not through anything in documents.py
    -- which is the point of declaring it on the constraint."""
    await _store(_write("gone"), _chunks(5))

    await docs_db.purge_stale(["something-else-entirely"], max_deletions=5)

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select count(*) as n from chunks")
        row = await cur.fetchone()

    assert row is not None
    assert row["n"] == 0


async def test_purge_refuses_when_discovery_collapsed() -> None:
    """The guard the n8n workflow does not have. If GitHub returns two documents
    instead of eleven, the other nine look deleted."""
    for doc_id in ("one", "two", "three", "four"):
        await _store(_write(doc_id), _chunks(1))

    with pytest.raises(PurgeSafetyError) as caught:
        await docs_db.purge_stale(["one"], max_deletions=1)

    assert caught.value.deletions == 3
    assert caught.value.stored == 4
    assert caught.value.limit == 1

    # And nothing was deleted -- the count and the delete share a transaction, and
    # the refusal happens between them.
    index = await docs_db.load_index()
    assert len(index) == 4


async def test_touch_marks_unchanged_documents_as_seen() -> None:
    """Without this, a document nobody edited keeps its old indexed_at and the
    purge treats it as deleted."""
    await _store(_write("unchanged"), _chunks(1))
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)

    await docs_db.touch_indexed_at(["unchanged"], later)

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select indexed_at from documents where doc_id = 'unchanged'")
        row = await cur.fetchone()

    assert row is not None
    assert row["indexed_at"] == later


async def _store_with_bad_vector() -> None:
    """Try to write a 128-dimensional vector into a vector(1536) column."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        document_id = await docs_db.upsert_document(conn, _write(), dt.datetime.now(dt.UTC))
        await docs_db.replace_chunks(conn, document_id, _chunks(1), [[0.1] * 128], "test-model")


async def test_a_wrong_width_vector_is_refused_by_the_column() -> None:
    """The last line of defence behind the check in embeddings.py.

    A vector column accepts any vector of the right width regardless of what it
    means, so the width is the only thing the database is able to verify -- and it
    does. Worth knowing where the floor is: everything above this has to catch a
    wrong *model*, because nothing below it can.
    """
    with pytest.raises(DataError, match="expected 1536 dimensions"):
        await _store_with_bad_vector()
