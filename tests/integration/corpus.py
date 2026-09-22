"""Planting a small knowledge base in the throwaway schema.

Retrieval tests need vectors whose ranking is known in advance, so these are not
realistic embeddings: each one points along a single axis, or between two of them.
Two vectors on different axes are at right angles and score 0; the same axis scores
1. That makes "which chunk comes back first" arithmetic rather than a guess.
"""

import datetime as dt

from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.ingestion.chunking import Chunk
from portfolio_ai.ingestion.frontmatter import DocumentMeta

DIMENSIONS = 1536


def axis(index: int) -> list[float]:
    """A unit vector along one axis. Cosine similarity to another axis is 0."""
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


def between(first: int, second: int, weight: float) -> list[float]:
    """A unit vector leaning ``weight`` of the way from one axis towards another.

    Similarity to ``axis(first)`` is ``sqrt(1 - weight ** 2)``, so weight=0.6 scores
    0.8 -- close, but behind anything sitting exactly on the axis.
    """
    vector = [0.0] * DIMENSIONS
    vector[first] = (1 - weight**2) ** 0.5
    vector[second] = weight
    return vector


async def seed(
    doc_id: str,
    title: str,
    sections: list[tuple[str, str, list[float]]],
    *,
    url: str | None = None,
) -> int:
    """Store one document with its chunks. Sections are (title, content, vector)."""
    chunks = [
        Chunk(
            index=index,
            section=f"section-{index}",
            section_title=section_title,
            content=content,
        )
        for index, (section_title, content, _) in enumerate(sections)
    ]
    write = docs_db.DocumentWrite(
        meta=DocumentMeta(doc_id=doc_id, title=title, url=url or f"https://mihaylov.io/#{doc_id}"),
        source_path=f"knowledgebase/{doc_id}.md",
        git_blob_sha=f"blob-{doc_id}",
        content_hash=f"hash-{doc_id}",
    )

    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        document_id = await docs_db.upsert_document(conn, write, dt.datetime.now(dt.UTC))
        await docs_db.replace_chunks(
            conn, document_id, chunks, [vector for _, _, vector in sections], "test-model"
        )

    return document_id


async def clear() -> None:
    """Empty the tables these tests write to, leaving the schema in place."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from documents")
        await conn.execute("delete from chat_sessions")
