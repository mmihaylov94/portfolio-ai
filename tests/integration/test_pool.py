"""The connection pool, against a real Postgres.

These cannot be unit tests. The pool's whole job is talking to another program,
and a fake Postgres would only prove that the fake behaves as written.
"""

import pytest

from portfolio_ai.db.pool import current_search_path, get_pool, health_check

# Applies to every test in this module, so none of them run in the default suite.
pytestmark = pytest.mark.integration


async def test_health_check_passes_against_a_live_database() -> None:
    assert await health_check() is True


async def test_search_path_points_at_the_test_schema(test_schema: str) -> None:
    """The fixture's schema name arrives as a parameter, matched by name.

    ``test_schema`` is the session fixture in conftest.py. Asking for it by
    naming it is the whole mechanism -- no import, no registration.
    """
    path = await current_search_path()

    assert path.startswith(test_schema)
    # public stays on the path so the vector type remains reachable, which is the
    # bug from lesson 6 that surfaced as a pool timeout.
    assert "public" in path


async def test_parameters_are_not_parsed_as_sql() -> None:
    """The claim from lesson 6, as an assertion rather than a demonstration."""
    pool = await get_pool()
    hostile = "x' OR '1'='1"

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select %s as value", (hostile,))
        row = await cur.fetchone()

    # It came back as text. It was never anything but text.
    assert row is not None
    assert row["value"] == hostile


async def test_rows_are_dictionaries_not_tuples() -> None:
    """row_factory=dict_row, so reordering a SELECT cannot silently change meaning."""
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select 1 as first, 2 as second")
        row = await cur.fetchone()

    assert row == {"first": 1, "second": 2}
