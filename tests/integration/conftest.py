"""Fixtures for tests that need a real Postgres.

Every test in this directory runs against a schema created just for the test run
and dropped afterwards. The real ``portfolio_rag`` schema is never touched.

The schema is built by running the actual migrations, not by a separate "test
schema" definition. A test schema that is maintained by hand drifts from the real
one, and then the tests pass against a shape production never has.
"""

import os
import secrets
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from portfolio_ai.config import get_settings
from portfolio_ai.db.pool import close_pool

# Every schema this suite creates starts with this, so orphans from a crashed run
# are identifiable and can be swept up.
SCHEMA_PREFIX = "pytest_"


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _sync_connect() -> psycopg.Connection:
    """A plain synchronous connection, for the fixture's own housekeeping.

    Deliberately not the async pool. This fixture runs before any test and after
    the last one, creating and dropping schemas -- work that has nothing to
    overlap with and no reason to involve an event loop. Alembic is synchronous
    for the same reason, which keeps the whole setup path free of the event loop
    questions from lesson 6.
    """
    return psycopg.connect(get_settings().database_url, autocommit=True)


def _drop_schema(name: str) -> None:
    with _sync_connect() as conn, conn.cursor() as cur:
        cur.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name)))


def _drop_orphan_schemas() -> None:
    """Remove schemas left behind by a run that crashed before its teardown."""
    with _sync_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select nspname from pg_namespace where nspname like %s",
            (f"{SCHEMA_PREFIX}%",),
        )
        for (name,) in cur.fetchall():
            cur.execute(sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name)))


@pytest.fixture(scope="session", autouse=True)
def test_schema() -> Iterator[str]:
    """Create a throwaway schema, migrate it, and drop it when the run ends.

    Session scope, so this happens once however many tests run. Building it per
    test would mean running three migrations before each one, which would make
    the suite slow enough that nobody would run it -- the practical meaning of
    fixture scope.
    """
    # This fixture creates and drops schemas, so it refuses to run anywhere that
    # is not explicitly a local environment. The cost of being wrong is dropping
    # things from a real database, which is not a risk worth carrying for the
    # sake of one guard clause.
    settings = get_settings()
    if settings.environment != "local":
        pytest.exit(
            f"Refusing to run integration tests with ENVIRONMENT={settings.environment!r}. "
            "These tests create and drop schemas.",
            returncode=1,
        )

    _drop_orphan_schemas()

    schema = f"{SCHEMA_PREFIX}{secrets.token_hex(4)}"

    # Point the whole project at the throwaway schema. Settings are cached, so the
    # cache has to be cleared for the new value to be seen -- see the lesson.
    os.environ["DB_SCHEMA"] = schema
    get_settings.cache_clear()

    # Alembic creates the schema itself, because env.py has to do that before it
    # can put its version table inside it. Lesson 7's chicken-and-egg fix pays for
    # itself here: the fixture gets schema creation for free.
    config = Config(os.path.join(_project_root(), "alembic.ini"))
    config.set_main_option("script_location", os.path.join(_project_root(), "migrations"))
    command.upgrade(config, "head")

    yield schema

    _drop_schema(schema)
    os.environ.pop("DB_SCHEMA", None)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _reset_pool() -> None:
    """Close the connection pool after every test.

    The pool is a module-level global (lesson 6), which is convenient in a running
    program and awkward here: a pool opened by one test is attached to that test's
    event loop, and using it from the next test would fail in a way that reads
    like a database problem rather than a lifecycle one.

    Closing it after each test costs a few connections' worth of setup and removes
    the whole category. ``autouse`` means no test has to remember.
    """
    yield
    await close_pool()
