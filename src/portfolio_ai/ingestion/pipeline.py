"""The ingestion run, start to finish.

    discover   ask GitHub what exists, with a blob SHA for each file
    diff       compare those SHAs against the database
    fetch      download only what changed
    parse      frontmatter into metadata, or reject the document
    chunk      split the body on H2 headings
    ---------- everything above is `plan()`; nothing has been written or spent
    embed      one batched call per document's chunks
    persist    per document, in one transaction: upsert, replace chunks
    purge      delete documents that no longer exist upstream
    report     counts, timings, tokens and real cost, as one log line

The split at the line is the shape of the whole module. ``plan()`` is read-only:
it talks to GitHub and reads the database, and it is exactly what ``--dry-run``
executes. ``execute()`` is everything that costs money or changes state. Having
the dry run be *the same code path* rather than a parallel implementation is what
makes it trustworthy -- a dry run that runs different code can only tell you about
itself.
"""

import datetime as dt
import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal

import structlog

from portfolio_ai.config import get_settings
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import DocumentRejectedError, PurgeSafetyError
from portfolio_ai.ingestion import github
from portfolio_ai.ingestion.chunking import Chunk, chunk_document
from portfolio_ai.ingestion.frontmatter import parse_document
from portfolio_ai.llm.embeddings import embed_texts
from portfolio_ai.llm.pricing import cost_usd, estimate_tokens

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Rejection:
    """A source file that could not be read, and what became of its old version."""

    path: str
    reason: str
    # The doc_id of the version already in the index, if there is one. Not None
    # means the article is still answering questions from its last good version;
    # None means the file has never been indexed and is simply absent. The
    # difference matters enough to report, because one is a stale article and the
    # other is a missing one.
    retained_doc_id: str | None

    def describe(self) -> str:
        if self.retained_doc_id is None:
            return f"{self.path}: {self.reason} (not indexed)"
        return f"{self.path}: {self.reason} (previous version retained)"


@dataclass(frozen=True)
class PendingDocument:
    """One document that parsed cleanly and needs writing."""

    write: docs_db.DocumentWrite
    chunks: list[Chunk]

    @property
    def doc_id(self) -> str:
        return self.write.meta.doc_id


@dataclass
class IngestionPlan:
    """What a run intends to do, worked out without writing anything.

    Produced by :func:`plan`, consumed by :func:`execute`, and printed on its own
    by ``--dry-run``.
    """

    discovered: int = 0
    # How many documents were already indexed when this run started. Carried
    # because the purge limit is a share of it, and the purge happens after the
    # plan is handed over.
    stored_count: int = 0
    pending: list[PendingDocument] = field(default_factory=list)
    unchanged_doc_ids: list[str] = field(default_factory=list)
    seen_doc_ids: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    would_purge: list[str] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return sum(len(document.chunks) for document in self.pending)

    @property
    def estimated_tokens(self) -> int:
        return sum(
            estimate_tokens(chunk.content) for document in self.pending for chunk in document.chunks
        )


@dataclass
class IngestionReport:
    """What a run did. Printed by the CLI and logged as one JSON object."""

    discovered: int = 0
    unchanged: int = 0
    indexed: int = 0
    chunks_written: int = 0
    rejected: list[Rejection] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)
    total_tokens: int = 0
    cost: Decimal = Decimal(0)
    duration_ms: int = 0
    dry_run: bool = False

    def as_log_fields(self) -> dict[str, object]:
        return {
            "discovered": self.discovered,
            "unchanged": self.unchanged,
            "indexed": self.indexed,
            "chunks_written": self.chunks_written,
            "rejected": len(self.rejected),
            "purged": len(self.purged),
            "total_tokens": self.total_tokens,
            "cost_usd": str(self.cost),
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
        }


def content_hash(text: str) -> str:
    """A stable fingerprint of a file's contents.

    SHA-256 of the text, not of the chunks. It answers "has this file changed",
    which is a question about the source rather than about how it happens to be
    split -- so a change to chunking does not invalidate it, and that is exactly
    why ``--force`` exists.

    Two hashes are stored per document and they answer different questions. The git
    blob SHA comes from the tree listing and is free, so it decides whether to
    download at all. This one is computed from what actually arrived and is the
    authority after that.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def plan(*, force: bool = False, only: list[str] | None = None) -> IngestionPlan:
    """Work out what needs doing. Reads GitHub and the database; writes nothing.

    This is the whole of ``--dry-run``, and the first half of a real run.
    """
    settings = get_settings()
    result = IngestionPlan()

    async with github.build_client() as client:
        remote = await github.list_markdown_files(client)
        result.discovered = len(remote)
        stored = await docs_db.load_index()
        result.stored_count = len(stored)

        # The purge guard, applied early and optimistically. `len(remote)` is the
        # best case for how many documents survive -- every discovered file parses
        # cleanly -- so if even that would delete too many, the run is already lost
        # and there is no reason to fetch anything or spend on embeddings.
        #
        # The exact check happens again at the purge itself, against the documents
        # that actually survived. Optimistic here means this never aborts a run
        # that would have been fine.
        limit = docs_db.purge_limit(
            len(stored), settings.ingestion_max_purge_fraction, settings.ingestion_purge_grace
        )
        if len(stored) - len(remote) > limit:
            raise PurgeSafetyError(stored=len(stored), surviving=len(remote), limit=limit)

        # The diff, and the only thing it can use is the blob SHA -- nothing has
        # read a file yet. A file is downloaded when its path is one we have never
        # stored, when its recorded blob SHA differs, or when --force says so.
        to_fetch = [file for file in remote if force or _needs_fetch(file, stored)]
        bodies = await github.fetch_all(client, to_fetch)

    # doc_id -> the path that claimed it. Checked in both branches below, because
    # an unchanged file is never parsed and would otherwise be invisible to the
    # duplicate check -- letting a new file quietly take over the identity of an
    # article nobody had touched.
    claimed_doc_ids: dict[str, str] = {}

    def reject(path: str, reason: str, retained: str | None) -> None:
        log.error("document_rejected", path=path, reason=reason, retained_doc_id=retained)
        result.rejected.append(Rejection(path=path, reason=reason, retained_doc_id=retained))

    for file in remote:
        if file.path not in bodies:
            # Its blob SHA matched, so it was not downloaded. It still exists
            # upstream, so it counts as seen -- otherwise the purge would treat
            # every unedited article as deleted.
            record = stored.by_path[file.path]

            if record.doc_id in claimed_doc_ids:
                reject(
                    file.path,
                    f"doc_id {record.doc_id!r} is already used by {claimed_doc_ids[record.doc_id]}",
                    None,
                )
                continue

            claimed_doc_ids[record.doc_id] = file.path
            result.seen_doc_ids.append(record.doc_id)
            result.unchanged_doc_ids.append(record.doc_id)
            continue

        try:
            meta, body = parse_document(file.path, bodies[file.path])
        except DocumentRejectedError as exc:
            # One unusable file must not stop the other ten, least of all on a
            # scheduled run with nobody watching. It is named, logged and counted.
            #
            # And it counts as *seen*, which is the part that matters. The file
            # still exists upstream -- we simply could not read it this time -- so
            # leaving it out of seen_doc_ids would mark it deleted and purge the
            # last good version. A typo in one article's frontmatter would then
            # remove that article from the assistant's knowledge, reported only as
            # a line in a log naming a different thing.
            #
            # So the previously indexed version stays and keeps answering. Its
            # indexed_at is deliberately not advanced, so it still looks stale to
            # anything checking, and the run exits non-zero either way.
            previous = stored.by_path.get(file.path)
            if previous is not None:
                result.seen_doc_ids.append(previous.doc_id)
                claimed_doc_ids[previous.doc_id] = file.path

            reject(exc.path, exc.reason, previous.doc_id if previous else None)
            continue

        # Two files claiming the same doc_id. Without this the second one upserts
        # over the first: both are fetched, chunked and paid to embed, one
        # silently replaces the other's chunks, and the run reports indexing two
        # documents while storing one. Nothing errors, and the losing article is
        # simply absent from the index.
        #
        # `remote` is sorted by path, so which file wins is at least deterministic
        # rather than whatever order GitHub happened to return.
        if meta.doc_id in claimed_doc_ids:
            reject(
                file.path,
                f"doc_id {meta.doc_id!r} is already used by {claimed_doc_ids[meta.doc_id]}",
                None,
            )
            continue

        claimed_doc_ids[meta.doc_id] = file.path

        if only and meta.doc_id not in only:
            continue

        result.seen_doc_ids.append(meta.doc_id)
        digest = content_hash(bodies[file.path])
        previous = stored.by_doc_id.get(meta.doc_id)

        if not force and previous is not None and _is_unchanged(previous, digest, file.path):
            result.unchanged_doc_ids.append(meta.doc_id)
            continue

        result.pending.append(
            PendingDocument(
                write=docs_db.DocumentWrite(
                    meta=meta,
                    source_path=file.path,
                    git_blob_sha=file.blob_sha,
                    content_hash=digest,
                ),
                chunks=chunk_document(body),
            )
        )

    result.would_purge = [
        doc_id for doc_id in stored.by_doc_id if doc_id not in result.seen_doc_ids
    ]

    return result


async def execute(plan_: IngestionPlan, *, only: list[str] | None = None) -> IngestionReport:
    """Embed, store and purge, following a plan. Everything that costs money."""
    settings = get_settings()
    indexed_at = dt.datetime.now(dt.UTC)

    report = IngestionReport(
        discovered=plan_.discovered,
        unchanged=len(plan_.unchanged_doc_ids),
        rejected=list(plan_.rejected),
    )

    # One document at a time. Embedding the whole corpus in a single call would
    # save a round trip and would mean a failure halfway through leaves nothing
    # written; per document, a failure leaves every earlier document correct.
    for document in plan_.pending:
        if not document.chunks:
            log.warning("document_has_no_chunks", doc_id=document.doc_id)

        embedding = await embed_texts([chunk.content for chunk in document.chunks])
        report.total_tokens += embedding.total_tokens
        report.cost += embedding.cost

        pool = await get_pool()
        # One transaction per document, so the upsert and the chunk swap land
        # together or not at all. Without it a failure between them leaves a
        # document with no chunks -- invisible to retrieval, perfectly healthy
        # looking in the documents table.
        async with pool.connection() as conn, conn.transaction():
            document_id = await docs_db.upsert_document(conn, document.write, indexed_at)
            await docs_db.replace_chunks(
                conn, document_id, document.chunks, embedding.vectors, embedding.model
            )

        report.indexed += 1
        report.chunks_written += len(document.chunks)
        log.info(
            "document_indexed",
            doc_id=document.doc_id,
            chunks=len(document.chunks),
            tokens=embedding.total_tokens,
        )

    await docs_db.touch_indexed_at(plan_.unchanged_doc_ids, indexed_at)

    # --only is a debugging tool that deliberately skips documents, so the set of
    # doc_ids seen is not the set that exists -- purging against it would delete
    # everything that was skipped.
    if only:
        log.info("purge_skipped", reason="--only restricts the run to named documents")
    else:
        report.purged = await docs_db.purge_stale(
            plan_.seen_doc_ids,
            max_deletions=docs_db.purge_limit(
                plan_.stored_count,
                settings.ingestion_max_purge_fraction,
                settings.ingestion_purge_grace,
            ),
        )

    return report


async def run(
    *,
    dry_run: bool = False,
    force: bool = False,
    only: list[str] | None = None,
) -> IngestionReport:
    """Run one ingestion pass.

    ``dry_run`` stops after planning: nothing is written and nothing is spent.
    ``force`` re-fetches and re-embeds everything regardless of hashes, which is
    what you run after changing how chunking works. ``only`` restricts the run to
    named ``doc_id`` values and skips the purge.
    """
    settings = get_settings()
    started = time.perf_counter()

    plan_ = await plan(force=force, only=only)

    if dry_run:
        report = IngestionReport(
            discovered=plan_.discovered,
            unchanged=len(plan_.unchanged_doc_ids),
            indexed=len(plan_.pending),
            chunks_written=plan_.chunk_count,
            rejected=list(plan_.rejected),
            purged=list(plan_.would_purge),
            total_tokens=plan_.estimated_tokens,
            cost=cost_usd(settings.embedding_model, plan_.estimated_tokens),
            dry_run=True,
        )

        for document in plan_.pending:
            log.info(
                "would_index",
                doc_id=document.doc_id,
                path=document.write.source_path,
                chunks=len(document.chunks),
            )

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        log.info("ingestion_plan", **report.as_log_fields())
        return report

    report = await execute(plan_, only=only)
    report.duration_ms = int((time.perf_counter() - started) * 1000)
    log.info("ingestion_complete", **report.as_log_fields())

    return report


def _needs_fetch(file: github.RemoteFile, stored: docs_db.DocumentIndex) -> bool:
    """Whether a file has to be downloaded, judged only on its git blob SHA.

    Runs before anything has been read, so a path with no record is always
    fetched -- which is correct, and is what happens to every file on a first run.
    """
    record = stored.by_path.get(file.path)

    if record is None:
        return True

    return record.git_blob_sha != file.blob_sha


def _is_unchanged(previous: docs_db.IndexedDocument, digest: str, path: str) -> bool:
    """Whether a downloaded file is genuinely identical to what is stored.

    The content hash is the main test. The path is checked too, because a renamed
    file has identical content at a new location: re-embedding is unnecessary, but
    leaving ``source_path`` stale would break the next run's fetch decision, so it
    is re-indexed rather than skipped. Fractions of a cent, and the alternative is
    a special case that exists to update one column.
    """
    return previous.content_hash == digest and previous.source_path == path
