"""The FastAPI application: assembled here, configured by the environment at startup.

``create_app()`` builds the app and reads no settings, so importing this module
works on a machine with no configuration -- the project rule, and what lets tests
build a fresh app each. Settings are read in the *lifespan*: the code FastAPI runs
once when the server starts, before the first request, and once when it stops.
That is where a bad configuration stops the process, and where the connections
are opened and closed.

Development::

    uv run uvicorn portfolio_ai.api.main:app --reload

Production runs ``python -m portfolio_ai.api`` instead; see ``__main__.py``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from portfolio_ai.api import errors
from portfolio_ai.api.middleware import RequestLogMiddleware
from portfolio_ai.api.routers import chat, feedback, health
from portfolio_ai.api.security import require_api_secrets
from portfolio_ai.api.turns import Turns
from portfolio_ai.config import get_settings
from portfolio_ai.db.pool import close_pool, get_pool
from portfolio_ai.llm.client import close_client
from portfolio_ai.logging import configure_logging

log = structlog.get_logger(__name__)

# How long shutdown waits for answers still being written before cancelling them.
# It runs after the server has stopped taking requests and has waited for open
# ones -- 30 s, set in __main__.py -- and the whole of it has to fit inside the
# 45 s Docker allows before it kills the container (docker-compose.yml).
DRAIN_GRACE_SECONDS = 10


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup before the ``yield``, shutdown after it.

    ``@asynccontextmanager`` turns a generator with one ``yield`` into an ``async
    with`` block, and FastAPI runs the whole server inside it. The ``finally`` is
    what makes the shutdown half reliable: it runs however the server stops.
    """
    # Any of these raising stops the process before it accepts a request: a bad
    # .env, a missing API key or hashing key.
    settings = get_settings()
    configure_logging()
    require_api_secrets(settings)

    # Opened now rather than on the first request, so the first visitor does not
    # wait for the connections. If the database is down, this does not fail -- the
    # pool keeps trying in the background -- and /readyz says so.
    await get_pool()
    log.info("api_started", environment=settings.environment)

    try:
        yield
    finally:
        turns: Turns = app.state.turns
        turns.closing = True
        await turns.drain(DRAIN_GRACE_SECONDS)
        # Only after the turns: they are what uses these.
        await close_client()
        await close_pool()
        log.info("api_stopped")


def create_app() -> FastAPI:
    """Build the app. Reads no settings and opens nothing; the lifespan does that."""
    app = FastAPI(
        title="Portfolio AI",
        summary="Rachel, the assistant on mihaylov.io. Private: called only by the site's API.",
        lifespan=lifespan,
    )

    # One registry per app, so each test's app has its own.
    app.state.turns = Turns()

    # Middleware wraps everything registered after it, so this sees every request,
    # including the ones that fail before reaching a route.
    app.add_middleware(RequestLogMiddleware)
    errors.install(app)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(feedback.router)
    return app


# What `uvicorn portfolio_ai.api.main:app` looks for.
app = create_app()
