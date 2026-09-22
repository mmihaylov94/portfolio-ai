"""The connection pool, and the setup every connection needs.

Postgres is a separate program. Everything this project does with it amounts to
sending a message down a connection and waiting for the reply -- which is why the
database layer is async, and why connections are pooled rather than opened per
query.

A connection is not cheap. Opening one means a TCP handshake, authentication, and
a process allocated on the server; the server also caps how many can exist at once,
across everything sharing the instance. So the pool opens a few, hands them out on
request, and takes them back afterwards.

Every query in this project goes through here.
"""

import structlog
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection, OperationalError, sql
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from portfolio_ai.config import get_settings

log = structlog.get_logger(__name__)

# What kind of pool this is, spelled out once.
#
# The type parameter is not decoration. `kwargs={"row_factory": dict_row}` below is
# what makes rows arrive as dictionaries, but it says so in a plain dict, where no
# type checker can read it. Left to itself, mypy uses psycopg's declared default --
# AsyncConnection[TupleRow] -- and then rejects `row["status"]` in a file three
# directories away, describing a tuple nobody wrote.
#
# Note what did *not* happen: mypy never complained about the missing parameter,
# because the library supplies a default for it. Silence from a type checker is not
# agreement. It can mean it quietly filled in a blank you did not notice.
#
# Writing the alias down is a promise rather than a proof -- mypy believes it without
# checking that the kwargs match. They have to be kept in step by hand, which is why
# they sit next to each other.
type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]

# Created on first use and reused afterwards. A pool has to outlive any single
# function -- a CLI run uses it throughout, and the API will hold one for the
# lifetime of the process -- so it lives at module level rather than being passed
# down through every call.
_pool: Pool | None = None


async def _configure_connection(conn: AsyncConnection[DictRow]) -> None:
    """Prepare a newly opened connection. Runs once per connection, not per query.

    Two things happen here, and both are per-connection state rather than anything
    that can be set globally.
    """
    settings = get_settings()

    async with conn.cursor() as cur:
        # The schema name comes from configuration, so it has to be put into the
        # query text somehow -- and it cannot be a parameter. Parameters carry
        # *values*; a schema name is part of the statement itself.
        #
        # sql.Identifier is the safe way to do that: it quotes and escapes, so
        # even a hostile value becomes a harmless (if absurd) identifier rather
        # than executable SQL. An f-string here would be the real thing the
        # project's "never build SQL by concatenation" rule is about.
        #
        # `public` stays on the path, second. Our tables win any name collision
        # because our schema is listed first, but extensions installed into public
        # -- pgvector among them -- remain reachable. Dropping public here fails
        # in a thoroughly confusing way: the next line cannot find the vector type,
        # every connection dies during setup, and the pool reports a timeout
        # rather than the actual error.
        await cur.execute(
            sql.SQL("set search_path to {}, {}").format(
                sql.Identifier(settings.db_schema),
                sql.Identifier("public"),
            )
        )

    # Teach this connection about the vector type, so a Python list can be sent to
    # a vector column and a vector column comes back as something useful. Needs the
    # extension to exist, which the first migration guarantees.
    await register_vector_async(conn)

    # psycopg 3 opens a transaction as soon as you execute anything, and it stays
    # open until something commits or rolls back. The two statements above put this
    # connection inside one, and the pool refuses a connection handed back that way:
    #
    #     connection left in status INTRANS by configure function: discarded
    #
    # It then discards it, tries again, discards that one too, and eventually
    # reports a pool timeout -- an error that describes the symptom and says
    # nothing about the cause. Ending the transaction here is the whole fix.
    await conn.commit()


async def get_pool() -> Pool:
    """Return the pool, opening it on first call."""
    # ruff dislikes `global` on principle, and for most code it is right: a function
    # that reassigns module state is hard to reason about and impossible to run twice.
    # Here it is the point. Lesson 8 paid the bill for it in the test fixtures, and
    # the alternative -- threading the pool through every signature in the project --
    # is a cost on every call for a benefit only the tests would see.
    global _pool  # ruff: ignore[global-statement]

    if _pool is None:
        settings = get_settings()
        _pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=settings.db_pool_max_size,
            configure=_configure_connection,
            # Rows come back as dictionaries keyed by column name rather than as
            # tuples. Tuples are positional, so reordering a SELECT silently
            # changes what row[0] means; a dictionary just keeps working.
            kwargs={"row_factory": dict_row},
            # Opening inside the constructor is deprecated for async pools, since
            # there is no event loop running yet to do it on.
            open=False,
        )
        await _pool.open()
        log.info("pool_opened", max_size=settings.db_pool_max_size, schema=settings.db_schema)

    return _pool


async def close_pool() -> None:
    """Close the pool and forget it. Safe to call when nothing was ever opened."""
    global _pool  # ruff: ignore[global-statement] -- see get_pool

    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("pool_closed")


async def current_search_path() -> str:
    """Report the search path the connection is actually using.

    Small, but worth having: queries in this project name tables without a schema
    prefix, which only works because :func:`_configure_connection` set the path.
    This is how you check that it did.

    It is also the pattern every database call in the project follows, so it is
    worth reading closely:

    - ``pool.connection()`` borrows a connection and gives it back afterwards,
      including if the body raises
    - ``conn.cursor()`` gets a cursor, which is the thing that actually runs a
      statement and holds the results
    - ``await cur.execute(...)`` sends the message; ``await cur.fetchone()`` waits
      for a row

    Both are borrowed on one line, separated by a comma. Nesting two ``async with``
    blocks means exactly the same thing and costs a level of indentation.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("show search_path")
        row = await cur.fetchone()

    return str(row["search_path"]) if row else ""


async def health_check() -> bool:
    """Return True if the database answered. Used as the API's readiness probe.

    Two decisions here are worth more than the code, because both are easy to get
    wrong in ways that only show up when something is already broken.

    **Only database-unreachable errors are caught.** ``PoolTimeout`` means no
    connection could be obtained in time -- server gone, wrong address, pool
    exhausted. ``OperationalError`` means a connection that was working died
    underneath us. Both genuinely mean "not usable", so both return False.

    Everything else is allowed to escape. Catching ``Exception`` here would be
    tempting and quietly awful: a typo in the column name below would report a
    perfectly healthy database as broken, and nothing anywhere would say why. A
    health check is the one place you are expected to swallow errors, which makes
    it the easiest place to hide a bug from yourself.

    **The wait is short.** The pool would otherwise spend thirty seconds deciding
    it cannot reach anything, by which point whatever asked has given up. A
    readiness probe that is slow to say "no" is barely better than one that never
    answers.
    """
    pool = await get_pool()

    try:
        async with pool.connection(timeout=2) as conn, conn.cursor() as cur:
            await cur.execute("select 1 as status")
            row = await cur.fetchone()

        if row and row["status"] == 1:
            return True
    except (OperationalError, PoolTimeout):
        return False

    return False
