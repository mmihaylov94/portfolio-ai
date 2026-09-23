"""One log line per request, and an id that ties together everything logged serving it.

Written as plain ASGI rather than with Starlette's ``BaseHTTPMiddleware`` (the
``@app.middleware("http")`` decorator), and the difference matters here.

ASGI is the whole interface between the server and the app: an app is any
``async def app(scope, receive, send)``. ``scope`` describes the request,
``receive`` is awaited for incoming messages -- the body, or a disconnect -- and
``send`` is called with outgoing ones: first the status line and headers, then the
body in pieces. Middleware is an app that wraps another one and passes those three
through, looking at what goes by.

``BaseHTTPMiddleware`` hides that behind a friendlier request-in, response-out
function, and to do it runs the inner app in a task group of its own and wraps
``receive``. That changes what a disconnect does: an endpoint that would otherwise
run to completion when its client leaves can be cancelled. The JSON endpoint relies
on not being cancelled, so this middleware stays at the level of the protocol and
changes nothing about how the app runs.
"""

import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger(__name__)

# Docker probes /healthz every thirty seconds: 2,880 lines a day saying nothing.
# Logged at debug, so they vanish at the default level and appear when asked for.
_QUIET_ROUTES = frozenset({"/healthz", "/readyz"})


class RequestLogMiddleware:
    """Bind a request id for the duration of a request, and log how it went."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan events come through here too, and have no request to log.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        # contextvars hold values per task, the async equivalent of thread-locals.
        # Everything logged while serving this request -- including by the turn's
        # own task, which copies the context when it is created -- carries the id.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        # 500 until the app says otherwise. If the app raises before sending a
        # status line, the error middleware outside this one answers 500 through
        # the server's own send -- which this never sees -- so the default has to
        # be the truth for that case.
        response_status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                # int() because it may be an HTTPStatus member, which is an int but
                # logs as "<HTTPStatus.OK: 200>" in the development console.
                response_status = int(message["status"])
                # The same id on the response, so a line in the proxy's log can be
                # matched with ours.
                MutableHeaders(scope=message).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            # The route that matched, as the code spells it --
            # "/v1/messages/{message_id}/feedback" -- rather than the path that was
            # asked for. A path holds whatever the caller put in it, so a proxy that
            # ever let a visitor's text into a URL would put it in this log too.
            # FastAPI records the matched route on the scope, which is the same dict
            # this middleware passed down. None means no route matched at all.
            route = getattr(scope.get("route"), "path", None)
            report = log.debug if route in _QUIET_ROUTES else log.info
            # Method, route and outcome. Never the body -- that is a visitor's words.
            report(
                "request",
                method=scope["method"],
                route=route,
                status=response_status,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
