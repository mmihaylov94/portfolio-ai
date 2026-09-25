"""The migrations, run for real against a throwaway schema.

Not a test of Alembic -- a test that *our* migrations produce the schema
ARCHITECTURE.md section 5 describes. The fixture in conftest.py has already run
``alembic upgrade head`` by the time any of this executes.
"""

import pytest
from psycopg.rows import DictRow

from portfolio_ai.db.pool import get_pool

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "documents",
    "chunks",
    "chat_sessions",
    "chat_messages",
    "message_feedback",
    "content_gaps",
    "eval_datasets",
    "eval_cases",
    "eval_runs",
    "eval_results",
}


async def _fetch_all(query: str, params: tuple[object, ...] = ()) -> list[DictRow]:
    # DictRow, not dict[str, object]. The two look interchangeable and are not:
    # `object` means "something, but nothing is known about it", so mypy correctly
    # refuses `"hnsw" in row["indexdef"]` -- you cannot search inside a value whose
    # type says it might be an int. DictRow is dict[str, Any], which is the truth
    # about a database row: the columns are strings, the values are whatever the
    # query selected and nobody can know that statically.
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(query, params)
        return list(await cur.fetchall())


async def test_every_expected_table_exists(test_schema: str) -> None:
    rows = await _fetch_all(
        "select tablename from pg_tables where schemaname = %s",
        (test_schema,),
    )
    tables = {row["tablename"] for row in rows}

    # A subset check rather than equality: alembic_version is there too, and a
    # test that breaks when an unrelated table appears is a test that gets edited
    # rather than read.
    assert tables >= EXPECTED_TABLES


async def test_alembic_recorded_the_latest_revision() -> None:
    # Unqualified on purpose. The search path already points at the test schema,
    # so this resolves there -- and writing it as an f-string would put a name
    # into SQL by string concatenation, which is the habit lesson 6 exists to
    # break. Even with a value we generated ourselves, a teaching repository
    # should not contain the pattern.
    rows = await _fetch_all("select version_num from alembic_version")

    assert [row["version_num"] for row in rows] == ["0005"]


async def test_chunks_cascade_when_a_document_is_deleted() -> None:
    """A behaviour, not a definition.

    Checking the constraint exists would prove the migration said the words.
    Deleting a document and finding the chunk gone proves the database agrees.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("""
            insert into documents (doc_id, title, source_path, content_hash, indexed_at)
            values ('cascade-test', 'Cascade', 'x.md', 'hash', now())
            returning id
        """)
        row = await cur.fetchone()
        assert row is not None
        document_id = row["id"]

        await cur.execute(
            """
            insert into chunks (document_id, chunk_index, content, embedding, embedding_model)
            values (%s, 0, 'content', %s, 'test')
            """,
            (document_id, [0.0] * 1536),
        )

        await cur.execute("delete from documents where id = %s", (document_id,))
        await cur.execute("select count(*) as n from chunks where document_id = %s", (document_id,))
        remaining = await cur.fetchone()

    assert remaining is not None
    assert remaining["n"] == 0


async def test_vector_extension_and_index_exist(test_schema: str) -> None:
    """The vector extension is installed and the retrieval index is the right kind.

    The extension check is close to a formality -- `chunks` could not have been
    created without it -- but it documents the dependency.

    The index check is the one that earns its place. An index built with the
    wrong operator class is not rejected by Postgres; it is silently ignored.
    Retrieval keeps returning correct answers while reading every row in the
    table, and the only symptom is slowness arriving long after the change that
    caused it. Nothing else in the project would notice.

    The assertion deliberately looks for the *properties* -- HNSW, cosine --
    rather than matching the exact CREATE INDEX text. The index name is one
    Postgres generated, and tuning parameters like `m` and `ef_construction`
    would be appended to that text later. Matching it exactly would report those
    perfectly good changes as failures, and a test that cries wolf gets loosened
    until it stops meaning anything.

    Verified by breaking it: switching the migration to `vector_l2_ops` makes
    this fail, which is the only way to know the test does anything at all.
    """
    extensions = await _fetch_all("select extname from pg_extension")
    assert "vector" in [row["extname"] for row in extensions]

    indexes = await _fetch_all(
        "select indexdef from pg_indexes where schemaname = %s and tablename = %s",
        (test_schema, "chunks"),
    )
    definitions = [row["indexdef"] for row in indexes]

    assert any("hnsw" in d and "vector_cosine_ops" in d for d in definitions), (
        f"no HNSW cosine index on chunks.embedding; found: {definitions}"
    )
