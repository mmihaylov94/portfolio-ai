"""``python -m portfolio_ai.api`` -- the API as production runs it.

Why not the ``uvicorn portfolio_ai.api.main:app`` command that development uses?
Because the command-line version of uvicorn always installs its own logging, which
writes plain text to stderr, and nothing can turn that off from the command line.
Started from Python with ``log_config=None``, uvicorn installs nothing, its lines go
through the same JSON handler as everything else, and every line this container
writes is one JSON object -- the first one included.

The server's settings live here, in code, once. The alternative is the same five
flags repeated in the Dockerfile, both compose files and the docs, and slowly
drifting apart.
"""

import structlog
import uvicorn

from portfolio_ai.api.security import require_api_secrets
from portfolio_ai.config import get_settings
from portfolio_ai.exceptions import ConfigError
from portfolio_ai.logging import configure_logging

log = structlog.get_logger(__name__)

# Every interface *of the container*. Inside Docker that is the only way anything
# else on the network can reach it; the host publishes no port, so this is not
# listening on the server's own interfaces.
HOST = "0.0.0.0"  # ruff: ignore[hardcoded-bind-all-interfaces]
PORT = 8000

# On SIGTERM, how long uvicorn waits for open requests -- streams mid-answer --
# before cancelling them. An answer at the default reasoning effort takes about
# 27 s. Unset, uvicorn waits forever, Docker's patience runs out first, and the
# process is killed before the shutdown in main.py gets to run.
GRACEFUL_SHUTDOWN_SECONDS = 30

# How long an idle connection is kept open for the next request. Longer than the
# Node client's own idle timeout (four or five seconds), so the server is never
# the one to close a connection the proxy is just about to reuse -- which shows up
# as an occasional, unreproducible "socket hang up".
KEEP_ALIVE_SECONDS = 30

# The event loop, chosen here rather than left to uvicorn, whose choice depends on
# the machine: uvloop where it is installed (the production image), and on Windows
# the "Proactor" loop, which psycopg's async mode cannot run on at all -- the pool
# logs "Psycopg cannot use the 'ProactorEventLoop'" on every attempt to connect.
#
# portfolio_ai/__init__.py fixes the same problem for everything else by changing
# asyncio's default, but uvicorn builds its loop itself, before it imports the app,
# so that fix arrives too late here. Naming the loop class means one loop everywhere:
# the one the tests and the command-line tools run on, on every machine.
LOOP = "asyncio:SelectorEventLoop"


def main() -> int:
    # Logging first, at a fixed level, so that a broken configuration is reported
    # as JSON like everything else. The lifespan reconfigures it from LOG_LEVEL.
    configure_logging("INFO")

    try:
        require_api_secrets(get_settings())
    except ConfigError as exc:
        # Checked here as well as in the lifespan so a bad .env is one clear line
        # and exit code 1, instead of a server starting up and failing inside it.
        log.error("api_not_started", error=str(exc))
        return 1

    uvicorn.run(
        "portfolio_ai.api.main:app",
        host=HOST,
        port=PORT,
        log_config=None,
        # Replaced by RequestLogMiddleware, whose lines carry the request id.
        access_log=False,
        # Nothing in front of this service speaks for the client in X-Forwarded-*
        # headers; the visitor's address arrives in X-Visitor-IP (security.py).
        proxy_headers=False,
        # No "server: uvicorn" header. Nobody outside can reach this, and it is
        # still one fewer thing to say about what runs where.
        server_header=False,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        timeout_keep_alive=KEEP_ALIVE_SECONDS,
        loop=LOOP,
        # Fail to start if the lifespan fails, rather than serving without one.
        lifespan="on",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
