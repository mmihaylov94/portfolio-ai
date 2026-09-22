"""How Alembic connects and where it keeps its bookkeeping.

Three things happen here that the stock Alembic template does not do, and each
one is a problem this project actually hit.

1. The database URL comes from :class:`Settings`, not from ``alembic.ini``, so
   there is one source of it. It also has to be rewritten on the way through:
   SQLAlchemy reads ``postgresql://`` as "use psycopg2", which is not installed.

2. The schema is created before Alembic looks for its version table, because the
   version table lives *inside* that schema. No migration can do this -- the
   version table is created before the first migration runs.

3. The search path is set, so migrations can write ``create table documents``
   and have it land in the right schema without repeating the name on every
   statement. Same arrangement as the runtime pool in db/pool.py.

Note this runs **synchronously** while the rest of the project is async.
Migrations run once, at deploy, with nothing to overlap -- async would buy
nothing and cost the event loop handling from lesson 6.
"""

from alembic import context
from sqlalchemy import create_engine, text
from sqlalchemy.schema import CreateSchema

from portfolio_ai.config import get_settings
from portfolio_ai.logging import configure_logging

settings = get_settings()

# Alembic reports what it is doing through Python's built-in logging, which the
# stock template configures from the ini file. We deleted that section, so
# without this a migration run prints absolutely nothing -- unnerving for a deploy
# step, where silence and success look identical.
#
# Routing it through our own setup instead means migration output arrives as JSON
# on stdout like everything else, and the container picks it up the same way.
configure_logging()


def _sqlalchemy_url() -> str:
    """Name the driver explicitly in the URL.

    SQLAlchemy maps a bare ``postgresql://`` to psycopg2, the older driver, which
    this project does not install. The failure is a ``ModuleNotFoundError`` for a
    package nobody asked for, which is a confusing way to be told "say which
    driver you meant".

    ``DATABASE_URL`` stays in its ordinary form everywhere else, because psycopg
    reads it directly and has no such ambiguity.
    """
    url = settings.database_url
    prefix = "postgresql://"
    if url.startswith(prefix):
        return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def run_migrations_online() -> None:
    """Connect, prepare the schema, and run whatever has not run yet."""
    engine = create_engine(_sqlalchemy_url())

    with engine.connect() as connection:
        # Alembic will try to create portfolio_rag.alembic_version before running
        # anything, so the schema has to exist first. CreateSchema quotes the name
        # properly -- the same reason db/pool.py uses sql.Identifier rather than an
        # f-string.
        connection.execute(CreateSchema(settings.db_schema, if_not_exists=True))

        # With the path set, migrations can name tables unqualified and still
        # create them in the right place. public stays on the path so the vector
        # type stays reachable, exactly as in db/pool.py.
        connection.execute(text(f'set search_path to "{settings.db_schema}", public'))
        connection.commit()

        context.configure(
            connection=connection,
            # Keep Alembic's own table inside our schema. On the production server
            # this database is shared with n8n, and public is not ours to clutter.
            version_table_schema=settings.db_schema,
            # No models to compare against -- see the lesson. Autogenerate is not
            # available here, and these migrations are written by hand.
            target_metadata=None,
        )

        with context.begin_transaction():
            context.run_migrations()


# Alembic can also run "offline", printing SQL instead of executing it, for
# handing to a DBA. This project deploys by running migrations directly, so that
# mode is not wired up; `alembic upgrade --sql` will not work here.
run_migrations_online()
