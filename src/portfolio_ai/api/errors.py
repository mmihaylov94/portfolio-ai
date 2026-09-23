"""What a failure looks like from outside: a status code and a sentence. One place.

Everything the API can fail with is translated here and nowhere else, for two
reasons. The status codes stay consistent -- "OpenAI is down" is a 503 whether it
happens before an answer starts or halfway through one -- and nothing that describes
the inside of the system reaches a response body. An OpenAI error message, a
psycopg one naming a host, a traceback: all of those go to the logs, and a visitor
gets a sentence written for a visitor.

There are two ways an error leaves the API, and both come through here:

- **As a response.** The handlers below are registered on the app by ``main.py``,
  and turn an exception raised anywhere in a request into a JSON body.
- **As an ``error`` event**, once a stream has started. By then the status line has
  gone out as 200 and cannot be changed, so ``routers/chat.py`` asks
  :func:`public_error` for the same status and sentence and puts them in the event.

That second case is why this is its own module rather than part of ``main.py``:
``main.py`` imports the routers, so a router importing ``main.py`` back would be a
circle.
"""

from dataclasses import dataclass
from http import HTTPStatus

import psycopg
import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from portfolio_ai.exceptions import AssistantError, ConfigError

log = structlog.get_logger(__name__)

UNAVAILABLE = "The assistant is unavailable right now. Please try again in a moment."
BROKEN = "Something went wrong on our side."


@dataclass(frozen=True)
class PublicError:
    """An error as the outside world is allowed to see it."""

    status: int
    detail: str


def public_error(exc: Exception) -> PublicError:
    """The status code and sentence for ``exc``.

    ``match`` with class patterns: ``case AssistantError():`` matches any instance
    of that class or a subclass, the same test as ``isinstance``, written as a list.
    """
    match exc:
        case AssistantError():
            # OpenAI failed after the SDK's own retries, or an answer ran past its
            # deadline. Not our bug, and likely to pass: 503 invites a retry.
            return PublicError(HTTPStatus.SERVICE_UNAVAILABLE, UNAVAILABLE)
        case psycopg.OperationalError():
            # The database is unreachable or the pool is exhausted. PoolTimeout is a
            # subclass, so it lands here too.
            return PublicError(HTTPStatus.SERVICE_UNAVAILABLE, UNAVAILABLE)
        case ConfigError():
            # Something in the deployment is wrong -- OpenAI rejected the key, say.
            # A retry will not help, so this is a 500 and an error in the logs.
            return PublicError(HTTPStatus.INTERNAL_SERVER_ERROR, BROKEN)
        case _:
            return PublicError(HTTPStatus.INTERNAL_SERVER_ERROR, BROKEN)


# The handlers are async with nothing to await, for the reason in security.py: a
# plain `def` handler would be run in a worker thread to build a small JSON body.
async def domain_error_handler(  # ruff: ignore[unused-async]
    request: Request, exc: Exception
) -> JSONResponse:
    """Turn an exception raised during a request into a JSON response.

    Registered for exactly the exception types above, so reaching it means the
    failure is one this project knows about. Anything else is a bug, and goes to
    :func:`unexpected_error_handler` instead.
    """
    error = public_error(exc)
    # The real error, for us: type and message, alongside the request id. Never the
    # visitor's text -- none of these exceptions carries it. A 503 is something
    # outside failing and is expected now and then; a 500 is ours to fix.
    report = log.warning if error.status == HTTPStatus.SERVICE_UNAVAILABLE else log.error
    report(
        "request_failed",
        status=int(error.status),
        error_type=type(exc).__name__,
        error=str(exc),
        path=request.url.path,
    )
    return JSONResponse(status_code=error.status, content={"detail": error.detail})


async def validation_error_handler(  # ruff: ignore[unused-async]
    request: Request,  # ruff: ignore[unused-function-argument] -- the handler signature
    exc: Exception,
) -> JSONResponse:
    """A 422 that says what was wrong without repeating what was sent.

    FastAPI's own 422 body echoes each rejected value back in an ``input`` field --
    here, that is the visitor's message. Harmless in a response to the same visitor,
    but it is exactly the kind of body a proxy writes to its logs when it sees an
    error, and a chat message does not belong in anybody's logs.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered for it
        raise exc
    problems = [
        {"loc": list(problem["loc"]), "msg": problem["msg"], "type": problem["type"]}
        for problem in exc.errors()
    ]
    return JSONResponse(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, content={"detail": problems})


async def unexpected_error_handler(  # ruff: ignore[unused-async]
    request: Request,  # ruff: ignore[unused-function-argument] -- the handler signature
    exc: Exception,
) -> JSONResponse:
    """A bug, answered in the same shape as every other error.

    Without this, an exception nobody planned for reaches Starlette's fallback, which
    answers ``Internal Server Error`` as plain text. A proxy that reads every error
    body as JSON would then fail on exactly the error it most needs to report.

    Starlette treats a handler registered for ``Exception`` differently from the
    rest. It runs in the outermost middleware, and the exception is raised again once
    the response has gone, so the server still logs the traceback -- with the request
    id, which is still bound. Running outside ``RequestLogMiddleware`` also means this
    response misses the ``X-Request-ID`` header that middleware adds, so it is added
    here: a 500 is the response somebody is most likely to want to trace.
    """
    error = public_error(exc)
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse(
        status_code=error.status,
        content={"detail": error.detail},
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def install(app: FastAPI) -> None:
    """Register the handlers. Called once, by ``create_app``."""
    for exc_class in (AssistantError, ConfigError, psycopg.OperationalError):
        app.add_exception_handler(exc_class, domain_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    # Everything else. Starlette runs this one outside all the middleware; see above.
    app.add_exception_handler(Exception, unexpected_error_handler)
