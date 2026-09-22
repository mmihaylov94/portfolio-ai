"""Reading and writing the knowledge base tables.

Every SQL statement touching ``documents`` and ``chunks`` is in this file. That is
the project convention and it has a practical reason beyond tidiness: the queries
here are the only place that knows the schema, so a migration that renames a column
has exactly one file to check rather than a codebase to search.

Parameters are always ``%s`` placeholders. Never an f-string, never concatenation
-- not even for values this project generated itself.
"""

import datetime as dt
import math
from dataclasses import dataclass

import structlog
from pgvector import Vector
from psycopg import AsyncConnection
from psycopg.rows import DictRow

from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import PurgeSafetyError
from portfolio_ai.ingestion.chunking import Chunk
from portfolio_ai.ingestion.frontmatter import DocumentMeta

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class IndexedDocument:
    """What the database already knows about one document.

    Used to decide whether a document needs re-fetching and re-embedding, so it
    carries the two hashes and the repository path they belong to.

    ``source_path`` is here for a reason worth stating: ``doc_id`` lives *inside*
    the file, so the only way to know which stored document a repository path
    corresponds to -- without downloading the file, which is the whole point -- is
    to have recorded the path last time.
    """

    doc_id: str
    source_path: str
    git_blob_sha: str | None
    content_hash: str


@dataclass(frozen=True)
class DocumentIndex:
    """The stored documents, looked up either way round.

    Two views over the same rows. ``by_doc_id`` answers "has this document's
    content changed"; ``by_path`` answers "do I need to download this file at all",
    which is asked before anything has been parsed and ``doc_id`` is unknown.
    """

    by_doc_id: dict[str, IndexedDocument]
    by_path: dict[str, IndexedDocument]

    def __len__(self) -> int:
        return len(self.by_doc_id)


async def load_index() -> DocumentIndex:
    """Every indexed document's identity, path and hashes.

    One query for the whole table rather than a lookup per document. The corpus is
    eleven rows; eleven round trips across the network to a database on another
    machine would take longer than the rest of discovery put together.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select doc_id, source_path, git_blob_sha, content_hash from documents")
        rows = await cur.fetchall()

    records = [
        IndexedDocument(
            doc_id=row["doc_id"],
            source_path=row["source_path"],
            git_blob_sha=row["git_blob_sha"],
            content_hash=row["content_hash"],
        )
        for row in rows
    ]

    return DocumentIndex(
        by_doc_id={record.doc_id: record for record in records},
        by_path={record.source_path: record for record in records},
    )


@dataclass(frozen=True)
class DocumentWrite:
    """Everything needed to write one document row, except the timestamp.

    Bundled rather than passed as six parameters, which is both easier to read at
    the call site and keeps the two hashes travelling together with the path they
    describe -- they are only meaningful as a set.
    """

    meta: DocumentMeta
    source_path: str
    git_blob_sha: str
    content_hash: str


async def upsert_document(
    conn: AsyncConnection[DictRow],
    document: DocumentWrite,
    indexed_at: dt.datetime,
) -> int:
    """Insert or update one document row, returning its primary key.

    ``on conflict (doc_id) do update`` is what makes ingestion idempotent. The
    alternative -- delete then insert, which is what the n8n workflow does -- gives
    the row a new primary key every run, so anything referencing it breaks. Nothing
    does yet; ``chunks.document_id`` will the moment this is called twice.

    Takes a connection rather than getting its own. The caller runs this and the
    chunk replacement below inside one transaction, and a function that opened its
    own connection could not participate in it.

    ``returning id`` gives back the primary key whether the row was inserted or
    updated. ``do update`` always updates the conflicting row -- even when every
    value is the same as before -- so the row is always there to return. It is ``do
    nothing`` that returns no row for one that already existed.
    """
    cursor = await conn.execute(
        """
        insert into documents (
            doc_id, source_type, page_type, title, url, tags, last_verified,
            source_file, source_path, git_blob_sha, content_hash, indexed_at
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (doc_id) do update set
            source_type   = excluded.source_type,
            page_type     = excluded.page_type,
            title         = excluded.title,
            url           = excluded.url,
            tags          = excluded.tags,
            last_verified = excluded.last_verified,
            source_file   = excluded.source_file,
            source_path   = excluded.source_path,
            git_blob_sha  = excluded.git_blob_sha,
            content_hash  = excluded.content_hash,
            indexed_at    = excluded.indexed_at,
            updated_at    = now()
        returning id
        """,
        (
            document.meta.doc_id,
            document.meta.source_type,
            document.meta.page_type,
            document.meta.title,
            document.meta.url,
            document.meta.tags,
            document.meta.last_verified,
            document.meta.source_file,
            document.source_path,
            document.git_blob_sha,
            document.content_hash,
            indexed_at,
        ),
    )
    row = await cursor.fetchone()

    if row is None:  # pragma: no cover - an upsert with `do update` always returns its row
        raise RuntimeError(f"upserting {document.meta.doc_id!r} returned no row")

    return int(row["id"])


async def replace_chunks(
    conn: AsyncConnection[DictRow],
    document_id: int,
    chunks: list[Chunk],
    vectors: list[list[float]],
    embedding_model: str,
) -> None:
    """Swap a document's chunks for a new set, inside the caller's transaction.

    Delete-then-insert rather than trying to match old chunks to new ones. Editing
    an article changes how many sections it has and what order they are in, so
    there is rarely a sensible correspondence -- and re-embedding is fractions of a
    cent, which makes cleverness here a cost with no benefit.

    Both statements are in the caller's transaction, which is what stops a failure
    between them leaving a document with no chunks at all. A document row whose
    chunks are missing is invisible to retrieval while looking completely healthy
    in the documents table.
    """
    await conn.execute("delete from chunks where document_id = %s", (document_id,))

    if not chunks:
        return

    # One round trip for every chunk in the document would be thirty or so
    # separate messages to a database on another machine. executemany pipelines
    # them into one exchange, which is a visible difference over a network and
    # invisible on localhost -- the reason it is worth doing here specifically.
    await conn.cursor().executemany(
        """
        insert into chunks (
            document_id, chunk_index, section, section_title, content,
            embedding, embedding_model
        )
        values (%s, %s, %s, %s, %s, %s, %s)
        """,
        [
            (
                document_id,
                chunk.index,
                chunk.section,
                chunk.section_title,
                chunk.content,
                vector,
                embedding_model,
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ],
    )


async def touch_indexed_at(doc_ids: list[str], indexed_at: dt.datetime) -> None:
    """Mark unchanged documents as seen in this run.

    Without this, a document nobody has edited would keep its old ``indexed_at``
    and the purge below would treat it as deleted. It is a single statement over a
    list rather than one per document -- ``= any(%s)`` takes an array parameter,
    which psycopg builds from a Python list.
    """
    if not doc_ids:
        return

    pool = await get_pool()

    async with pool.connection() as conn:
        await conn.execute(
            "update documents set indexed_at = %s, updated_at = now() where doc_id = any(%s)",
            (indexed_at, doc_ids),
        )


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk found by a similarity search, with the document it came from."""

    chunk_id: int
    doc_id: str
    title: str
    url: str | None
    # The section's slug ("php-laravel") and its heading ("Does Mihail use Laravel?").
    section: str | None
    section_title: str | None
    content: str
    # Cosine similarity to the query: 1 means pointing the same way, 0 unrelated.
    # In practice this corpus scores in a narrow band -- the best hit for a good
    # question lands around 0.5-0.7 -- which is why nothing here uses a fixed cutoff
    # yet. The analytics step calibrates one from real traffic.
    score: float


async def search_chunks(vector: list[float], *, top_k: int) -> list[RetrievedChunk]:
    """The ``top_k`` chunks closest to ``vector``, best first.

    ``<=>`` is pgvector's cosine *distance*, which is what the HNSW index from
    migration 0001 is built for (``vector_cosine_ops``) -- order by the same operator
    the index was built with, or the index is ignored and every row is scored.
    Distance runs from 0 (identical) upwards, so ``1 - distance`` turns it into the
    similarity people expect, where bigger is better.

    The query vector is wrapped in ``Vector`` rather than passed as a list. psycopg
    sends a Python list as ``float8[]``, and pgvector only converts arrays to vectors
    *on assignment* -- into a column, as ingestion does. In a comparison there is no
    ``vector <=> float8[]`` operator, and the query fails. ``Vector`` goes over the
    wire as a vector in the first place, through the adapter registered on every
    connection in ``pool.py``.

    Both ``%(query)s`` placeholders are one parameter, sent once. Named placeholders
    are what allow using a value twice without passing it twice.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select c.id, c.section, c.section_title, c.content,
                   d.doc_id, d.title, d.url,
                   1 - (c.embedding <=> %(query)s) as score
            from chunks c
            join documents d on d.id = c.document_id
            order by c.embedding <=> %(query)s
            limit %(top_k)s
            """,
            {"query": Vector(vector), "top_k": top_k},
        )
        rows = await cur.fetchall()

    return [
        RetrievedChunk(
            chunk_id=row["id"],
            doc_id=row["doc_id"],
            title=row["title"],
            url=row["url"],
            section=row["section"],
            section_title=row["section_title"],
            content=row["content"],
            score=float(row["score"]),
        )
        for row in rows
    ]


def purge_limit(stored: int, fraction: float, grace: int) -> int:
    """How many documents one run is allowed to delete.

    A share of what is stored, with a small floor so that a tiny corpus is not
    frozen -- a third of three documents is one, and refusing to let anyone remove
    a single article from a three-article knowledge base would be absurd.

    Worked through, at ``fraction=0.3`` and ``grace=2``::

        stored=11  limit=4    a routine tidy-up of 3 passes; a collapse to 6 does not
        stored=50  limit=15   which is the case a fixed floor of 8 would have missed
        stored=3   limit=2    the grace, not the share
        stored=0   limit=2    a first run, where there is nothing to delete anyway
    """
    return max(grace, math.ceil(stored * fraction))


async def purge_stale(seen_doc_ids: list[str], *, max_deletions: int) -> list[str]:
    """Delete documents that were not seen in this run. Returns what was removed.

    This is how a file deleted from the source repository leaves the index, and it
    is the single most dangerous statement in the project: if discovery returns two
    documents instead of eleven because GitHub had a bad minute, the other nine
    look deleted.

    So what *would* be deleted is counted first, inside the same transaction as the
    delete, and the run aborts if it is more than ``max_deletions``. Counting and
    deleting in one transaction matters: read the count on one connection and
    delete on another and the two can disagree, which is a guard that checks
    something other than what it then does.

    Chunks go with their documents through ``on delete cascade``, declared in
    migration 0001. Nothing here mentions the chunks table, and that is the point
    of the constraint: the database enforces it rather than this remembering to.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        # `!= all(%s)` rather than `not in (...)`: an array parameter is one
        # placeholder whatever the length, so the statement text never changes and
        # nothing is built by concatenation.
        await cur.execute("select doc_id from documents where doc_id != all(%s)", (seen_doc_ids,))
        stale = sorted(row["doc_id"] for row in await cur.fetchall())

        if len(stale) > max_deletions:
            # Raised inside the transaction, so it rolls back. Nothing has been
            # written at this point either way -- the rollback is belt and braces
            # rather than load bearing.
            raise PurgeSafetyError(
                stored=len(stale) + len(seen_doc_ids),
                surviving=len(seen_doc_ids),
                limit=max_deletions,
            )

        if stale:
            await cur.execute("delete from documents where doc_id != all(%s)", (seen_doc_ids,))

    if stale:
        log.info("purged_documents", count=len(stale), doc_ids=stale)

    return stale
