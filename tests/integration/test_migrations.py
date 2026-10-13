"""The migrations, run for real against a throwaway schema.

Not a test of Alembic -- a test that *our* migrations produce the schema
ARCHITECTURE.md section 5 describes. The fixture in conftest.py has already run
``alembic upgrade head`` by the time any of this executes.
"""

from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg.rows import DictRow

from portfolio_ai.config import get_settings
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

    assert [row["version_num"] for row in rows] == ["0006"]


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


# --- 0006: content gaps keep ids, not questions --------------------------------------


def _gap_columns(schema: str) -> dict[str, str]:
    """The columns of content_gaps and their types, read on a connection of its own.

    Synchronous, like Alembic and like the fixture that built the schema: the test
    that uses it runs migrations, and has no business inside an event loop.
    """
    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = %s and table_name = 'content_gaps'",
            (schema,),
        )
        return dict(cur.fetchall())


def _gap_default(schema: str, column: str) -> str | None:
    """What a column of content_gaps is given when an insert names no value for it."""
    with psycopg.connect(get_settings().database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "select column_default from information_schema.columns "
            "where table_schema = %s and table_name = 'content_gaps' and column_name = %s",
            (schema, column),
        )
        row = cur.fetchone()
    assert row is not None, f"content_gaps has no column {column}"
    default: str | None = row[0]
    return default


def test_content_gaps_holds_the_ids_of_questions_and_not_their_text(test_schema: str) -> None:
    """The column that must not exist is the point of the migration. The table is kept
    indefinitely, and a visitor's question copied into it would outlive the 90 days
    chat is kept for."""
    columns = _gap_columns(test_schema)

    assert "example_questions" not in columns
    assert columns["example_message_ids"] == "ARRAY"
    assert columns["fallback_count"] == "integer"
    assert columns["refreshed_at"] == "timestamp with time zone"
    assert columns["resolved_at"] == "timestamp with time zone"


def test_the_content_gaps_migration_goes_down_and_comes_back(test_schema: str) -> None:
    """Down to 0005 and up again, on the throwaway schema, with the table empty as it
    is everywhere this has run. A downgrade nobody has run is a downgrade nobody
    knows works.

    Not an async test, though every other one here is: Alembic is synchronous and
    drives its own connection, as it did when the fixture built this schema.
    """
    root = Path(__file__).resolve().parents[2]
    config = Config(root / "alembic.ini")
    config.set_main_option("script_location", str(root / "migrations"))

    command.downgrade(config, "0005")
    try:
        before = _gap_columns(test_schema)
        assert before["example_questions"] == "ARRAY"
        assert "example_message_ids" not in before
        assert "resolved_at" not in before
        # As migration 0002 declared it: with no default. The downgrade needs one
        # to add the column to rows that exist, and has to take it off again.
        assert _gap_default(test_schema, "example_questions") is None
    finally:
        # Whatever the assertions said: every test after this one needs the schema
        # back at head.
        command.upgrade(config, "head")

    assert "example_message_ids" in _gap_columns(test_schema)
