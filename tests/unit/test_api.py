"""The HTTP API, in-process: every check in front of an answer, and both ways of getting one.

No database and no OpenAI. The assistant is replaced by a script -- a generator
yielding whatever events a test sets up -- and the two reads admission makes (recent
questions, today's spend) return values the test sets. Everything else is real:
routing, auth, validation, admission, the turn registry, SSE framing, error mapping.

The integration suite runs the same endpoints against a real database and the real
assistant, with only OpenAI faked.

Requests go through ``httpx.ASGITransport``, which calls the app directly in the
test's own event loop. It collects a response whole before returning it, so these
tests see what a stream *contained*, not when each part arrived; the disconnect test
further down drives the app by hand for the part where timing matters.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, MutableMapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from portfolio_ai.api import deps, main, middleware
from portfolio_ai.api.main import create_app
from portfolio_ai.api.routers import chat, health
from portfolio_ai.api.security import hash_ip, require_api_key
from portfolio_ai.api.turns import Turns
from portfolio_ai.assistant import conversation
from portfolio_ai.assistant.agent import (
    Citation,
    Classified,
    Done,
    Event,
    Searched,
    Token,
    TurnResult,
)
from portfolio_ai.assistant.prompts.loader import DAILY_LIMIT_REPLY
from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db import feedback as feedback_db
from portfolio_ai.db.chat import ClientInfo, RecentQuestions
from portfolio_ai.exceptions import AssistantError, ConfigError

KEY = "unit-tests-api-key-that-is-long-enough"
SALT = "unit-tests-ip-hashing-key-long-enough"
SESSION = "3f0c9a52-8d1e-4b7a-9c2f-6e5d4c3b2a10"
OTHER_SESSION = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MESSAGE_ID = 4812


def _result(reply: str, **overrides: Any) -> TurnResult:
    values: dict[str, Any] = {
        "reply": reply,
        "classification": "mihail_related",
        "citations": [
            Citation(
                doc_id="tech-stack",
                title="Tech Stack",
                url="https://mihaylov.io/#about",
                section="php-laravel",
            )
        ],
        "searches": [],
        "retrieved_chunk_ids": [1, 2],
        "top_score": 0.62,
        "fallback_used": False,
        "links_removed": 0,
        "calls": [],
        "model": "gpt-5-mini-2025-08-07",
        "latency_ms": 900,
        "first_token_ms": 400,
        "message_id": MESSAGE_ID,
    }
    return TurnResult(**{**values, **overrides})


def _answer(*pieces: str) -> list[Event]:
    """The events of an ordinary knowledge-base answer, streamed in these pieces."""
    return [
        Classified("mihail_related"),
        Searched(query="Laravel experience", chunks=[], top_score=0.62),
        *(Token(piece) for piece in pieces),
        Done(_result("".join(pieces))),
    ]


@dataclass
class Script:
    """What the stand-in assistant does when a turn starts."""

    events: list[Event] = field(default_factory=lambda: _answer("Yes, ", "he does."))
    # If set, the turn pauses after its first event until this is set.
    hold: asyncio.Event | None = None
    # Raised after the events, if set.
    error: Exception | None = None
    # One entry per turn started: (session_id, message, client).
    started: list[tuple[str, str, ClientInfo | None]] = field(default_factory=list)
    # The reply of every turn that ran to the end, as a stored turn would.
    finished: list[str] = field(default_factory=list)
    # The request id each turn would log under, read after its last event.
    request_ids: list[str | None] = field(default_factory=list)


@dataclass
class Limits:
    """What admission's two database reads return."""

    recent: RecentQuestions = field(default_factory=lambda: RecentQuestions(0, 0))
    spent: Decimal = Decimal(0)
    error: Exception | None = None
    reads: int = 0


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> Script:
    scripted = Script()

    async def chat(
        session_id: str,
        message: str,
        *,
        client: ClientInfo | None = None,
        config: object = None,  # ruff: ignore[unused-function-argument] -- chat()'s signature
    ) -> AsyncIterator[Event]:
        scripted.started.append((session_id, message, client))
        for number, event in enumerate(scripted.events):
            yield event
            if number == 0 and scripted.hold is not None:
                await scripted.hold.wait()
        scripted.request_ids.append(structlog.contextvars.get_contextvars().get("request_id"))
        if scripted.error is not None:
            raise scripted.error
        done = scripted.events[-1]
        if isinstance(done, Done):
            scripted.finished.append(done.result.reply)

    monkeypatch.setattr(conversation, "chat", chat)
    return scripted


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> Limits:
    current = Limits()

    # Both yield to the event loop once, as a real query would. The race test below
    # depends on it: without a real await, two requests could never interleave.
    async def recent_questions(
        session_id: str,  # ruff: ignore[unused-function-argument]
        *,
        window: object,  # ruff: ignore[unused-function-argument]
        limit: int,  # ruff: ignore[unused-function-argument]
    ) -> RecentQuestions:
        current.reads += 1
        await asyncio.sleep(0)
        if current.error is not None:
            raise current.error
        return current.recent

    async def spend_since(window: object) -> Decimal:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        return current.spent

    monkeypatch.setattr(chat_db, "recent_questions", recent_questions)
    monkeypatch.setattr(chat_db, "spend_since", spend_since)
    return current


@pytest.fixture(autouse=True)
def _api_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """On top of conftest.py's fixed settings, the two the API needs."""
    monkeypatch.setenv("PORTFOLIO_AI_API_KEY", KEY)
    monkeypatch.setenv("IP_HASH_SALT", SALT)
    get_settings.cache_clear()


def _set(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.fixture
async def app(
    script: Script,
    limits: Limits,  # ruff: ignore[unused-function-argument] -- requested for its patches
) -> AsyncIterator[FastAPI]:
    """A fresh app per test, so no test inherits another's running turns."""
    application = create_app()
    yield application
    if script.hold is not None:
        script.hold.set()
    await application.state.turns.drain(grace_seconds=5)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {KEY}"},
    ) as test_client:
        yield test_client


def _body(message: str = "Does Mihail work with Laravel?", **overrides: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "message": message, **overrides}


def _events(stream: str) -> list[tuple[str, Any]]:
    """Parse a server-sent event stream into (event, data) pairs, skipping pings."""
    parsed = []
    for block in stream.split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: "))
        if name is not None:
            parsed.append((name, data))
    return parsed


async def _until(condition: Callable[[], bool]) -> None:
    """Wait for something another task is doing, without sleeping a guessed amount.

    A polling loop, which ruff would rather were an Event. But the conditions are
    counts changed by the code under test, which has no event to set, and each
    sleep(0) hands the loop to the other tasks -- so the wait is as short as it can be.
    """
    async with asyncio.timeout(5):
        while not condition():  # ruff: ignore[async-busy-wait]
            await asyncio.sleep(0)


@dataclass
class LogRecorder:
    """Stands in for a module's logger and keeps each line as a dict.

    Patched onto the module rather than captured through structlog, whose
    configuration depends on which tests happened to run before this one.
    """

    lines: list[dict[str, Any]] = field(default_factory=list)

    def _record(self, event: str, **fields: Any) -> None:
        self.lines.append({"event": event, **fields})

    debug = info = warning = error = _record


# --- who may call -------------------------------------------------------------

CHAT_ROUTES = ["/v1/chat", "/v1/chat/stream"]
PROTECTED = [*CHAT_ROUTES, "/v1/messages/1/feedback"]


@pytest.mark.parametrize("path", PROTECTED)
@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer wrong-key-of-a-reasonable-length-here", "Basic dXNlcjpwYXNz", "Bearer"],
    ids=["missing", "wrong", "not-bearer", "empty"],
)
async def test_every_route_but_health_needs_the_key(
    client: httpx.AsyncClient, script: Script, path: str, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    client.headers.pop("Authorization")

    response = await client.post(path, json=_body(rating=1), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert script.started == []


async def test_a_key_that_is_not_ascii_is_a_401_not_a_500(
    client: httpx.AsyncClient,
) -> None:
    """compare_digest raises on non-ASCII str, so the check compares bytes."""
    response = await client.post(
        "/v1/chat",
        json=_body(),
        headers=[(b"authorization", "Bearer clé-inconnue".encode())],
    )

    assert response.status_code == 401


async def test_the_health_checks_need_no_key(client: httpx.AsyncClient) -> None:
    client.headers.pop("Authorization")

    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("database_answers", "status", "body"),
    [(True, 200, {"status": "ready"}), (False, 503, {"status": "unavailable"})],
    ids=["database-up", "database-down"],
)
async def test_ready_means_the_database_answers(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    database_answers: bool,
    status: int,
    body: dict[str, str],
) -> None:
    """The deploy script waits on this before it calls a release good."""

    async def health_check() -> bool:
        await asyncio.sleep(0)
        return database_answers

    monkeypatch.setattr(health, "health_check", health_check)
    client.headers.pop("Authorization")

    response = await client.get("/readyz")

    assert (response.status_code, response.json()) == (status, body)


async def test_with_no_key_configured_nothing_is_let_through(
    client: httpx.AsyncClient, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that somehow skipped startup fails closed: 500, never an open door."""
    monkeypatch.delenv("PORTFOLIO_AI_API_KEY")
    get_settings.cache_clear()

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 500
    assert script.started == []


# --- validation ---------------------------------------------------------------


def test_the_chat_routes_take_nothing_but_the_admission() -> None:
    """If an endpoint took its own parameters, FastAPI would validate them after
    admission had already started an answer -- and a 422 would abandon it.

    Read from the router rather than the app: since 0.141, an app keeps included
    routers as routers instead of copying their routes into ``app.routes``.
    """
    routes = [route for route in chat.router.routes if isinstance(route, APIRoute)]
    assert sorted(route.path for route in routes) == CHAT_ROUTES

    for route in routes:
        dependant = route.dependant
        assert [dependency.call for dependency in dependant.dependencies] == [
            require_api_key,
            deps.admit_turn,
        ], route.path
        assert not dependant.body_params
        assert not dependant.query_params
        assert not dependant.header_params


@pytest.mark.parametrize("path", CHAT_ROUTES)
@pytest.mark.parametrize(
    "body",
    [
        _body(""),
        _body("   \n  "),
        _body("x" * 2001),
        _body("before\x00after"),
        _body("a bell\x07"),
        _body(session_id="short"),
        _body(session_id="has spaces in it"),
        _body(session_id="x" * 101),
        _body(extra="field"),
        {"session_id": SESSION},
    ],
    ids=[
        "empty",
        "whitespace",
        "too-long",
        "nul",
        "control-character",
        "short-session",
        "session-with-spaces",
        "long-session",
        "unknown-field",
        "no-message",
    ],
)
async def test_an_invalid_request_starts_nothing(
    client: httpx.AsyncClient,
    script: Script,
    limits: Limits,
    path: str,
    body: dict[str, Any],
) -> None:
    response = await client.post(path, json=body)

    assert response.status_code == 422
    assert script.started == []
    assert limits.reads == 0, "rejected before admission ran at all"


async def test_a_422_does_not_repeat_what_was_sent(client: httpx.AsyncClient) -> None:
    """FastAPI echoes rejected values by default. A visitor's message has no place in
    an error body a proxy might log."""
    secret = "my phone number is 07700 900123 " + "x" * 2000

    response = await client.post("/v1/chat", json=_body(secret))

    assert response.status_code == 422
    assert "07700" not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "message"]


# --- admission ----------------------------------------------------------------


async def test_a_conversation_mid_answer_gets_a_409(
    client: httpx.AsyncClient, script: Script
) -> None:
    script.hold = asyncio.Event()
    first = asyncio.create_task(client.post("/v1/chat", json=_body()))
    await _until(lambda: len(script.started) == 1)

    second = await client.post("/v1/chat", json=_body("And Vue?"))

    assert second.status_code == 409
    script.hold.set()
    assert (await first).status_code == 200
    assert len(script.started) == 1


async def test_another_conversation_is_not_held_up(
    client: httpx.AsyncClient, script: Script
) -> None:
    script.hold = asyncio.Event()
    first = asyncio.create_task(client.post("/v1/chat", json=_body()))
    await _until(lambda: len(script.started) == 1)

    # The held turn blocks only its own conversation, so this one starts too -- and
    # waits on the same hold, which is released once both are running.
    second = asyncio.create_task(client.post("/v1/chat", json=_body(session_id=OTHER_SESSION)))
    await _until(lambda: len(script.started) == 2)
    script.hold.set()

    assert [(await first).status_code, (await second).status_code] == [200, 200]


async def test_two_first_messages_at_once_get_one_answer(
    client: httpx.AsyncClient, script: Script
) -> None:
    """Both pass the reads together; only one passes the check that follows them."""
    script.hold = asyncio.Event()
    requests = [
        asyncio.create_task(client.post("/v1/chat", json=_body(f"Question {n}"))) for n in (1, 2)
    ]

    done, _ = await asyncio.wait(requests, return_when=asyncio.FIRST_COMPLETED)
    script.hold.set()
    statuses = sorted([(await request).status_code for request in requests])

    assert statuses == [200, 409]
    assert len(script.started) == 1
    assert len(done) == 1


async def test_too_many_questions_is_a_429_saying_when_to_come_back(
    client: httpx.AsyncClient, script: Script, limits: Limits
) -> None:
    limits.recent = RecentQuestions(count=20, retry_after=37)

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "37"
    assert script.started == []


async def test_one_under_the_limit_is_answered(client: httpx.AsyncClient, limits: Limits) -> None:
    limits.recent = RecentQuestions(count=19, retry_after=0)

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 200


async def test_past_the_spending_limit_the_answer_is_a_polite_refusal(
    client: httpx.AsyncClient, script: Script, limits: Limits
) -> None:
    limits.spent = Decimal("1.00")  # the default cap, reached exactly

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 200, "a refusal, not an error"
    assert response.json() == {
        "reply": DAILY_LIMIT_REPLY.text,
        "session_id": SESSION,
        "message_id": None,
        "classification": None,
        "citations": [],
        "usage": None,
    }
    assert script.started == [], "no answer started, so nothing spent and nothing stored"


async def test_the_refusal_streams_like_an_answer(
    client: httpx.AsyncClient, limits: Limits
) -> None:
    limits.spent = Decimal("5.00")

    response = await client.post("/v1/chat/stream", json=_body())

    assert _events(response.text) == [
        ("token", {"text": DAILY_LIMIT_REPLY.text}),
        ("done", {"message_id": None, "classification": None, "citations": [], "usage": None}),
    ]


async def test_a_cap_of_zero_turns_the_chat_off(
    client: httpx.AsyncClient, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(monkeypatch, "DAILY_SPEND_CAP_USD", "0")

    response = await client.post("/v1/chat", json=_body())

    assert response.json()["reply"] == DAILY_LIMIT_REPLY.text
    assert script.started == []


async def test_too_many_answers_at_once_is_a_503(
    client: httpx.AsyncClient, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set(monkeypatch, "MAX_CONCURRENT_TURNS", "1")
    script.hold = asyncio.Event()
    first = asyncio.create_task(client.post("/v1/chat", json=_body()))
    await _until(lambda: len(script.started) == 1)

    second = await client.post("/v1/chat", json=_body(session_id=OTHER_SESSION))

    assert second.status_code == 503
    assert second.headers["retry-after"] == "5"
    script.hold.set()
    await first


async def test_nothing_starts_while_shutting_down(
    client: httpx.AsyncClient, app: FastAPI, script: Script
) -> None:
    turns: Turns = app.state.turns
    turns.closing = True

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 503
    assert script.started == []


@pytest.mark.parametrize("path", CHAT_ROUTES)
async def test_a_database_that_is_down_is_a_503_before_anything_starts(
    client: httpx.AsyncClient, script: Script, limits: Limits, path: str
) -> None:
    """Even on the streaming route: admission fails before a single byte is sent,
    so it is an ordinary JSON 503 rather than a stream that stops."""
    limits.error = PoolTimeout("couldn't get a connection after 30.00 sec")

    response = await client.post(path, json=_body())

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert "connection" not in response.text, "the reason stays in the logs"
    assert script.started == []


# --- answering ----------------------------------------------------------------


async def test_the_whole_answer_at_once(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 200
    assert response.json() == {
        "reply": "Yes, he does.",
        "session_id": SESSION,
        "message_id": MESSAGE_ID,
        "classification": "mihail_related",
        "citations": [
            {
                "doc_id": "tech-stack",
                "title": "Tech Stack",
                "url": "https://mihaylov.io/#about",
                "section": "php-laravel",
            }
        ],
        "usage": {
            "model": "gpt-5-mini-2025-08-07",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latency_ms": 900,
            "first_token_ms": 400,
        },
    }
    assert "cost" not in response.text


async def test_the_answer_as_it_is_written(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat/stream", json=_body())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [name for name, _ in events] == ["route", "search", "token", "token", "done"]
    assert events[0][1] == {"classification": "mihail_related"}
    assert "".join(data["text"] for name, data in events if name == "token") == "Yes, he does."
    assert events[-1][1]["message_id"] == MESSAGE_ID


async def test_a_search_event_says_nothing_about_the_search(
    client: httpx.AsyncClient, script: Script
) -> None:
    """The query is written by the model and never passes the link filter."""
    script.events = [
        Classified("mihail_related"),
        Searched(query="https://mihaylov.io/knowledgebase/secret", chunks=[], top_score=0.5),
        Token("Here."),
        Done(_result("Here.")),
    ]

    response = await client.post("/v1/chat/stream", json=_body())

    assert ("search", {}) in _events(response.text)
    assert "knowledgebase" not in response.text


async def test_where_the_visitor_came_from_is_recorded_hashed_and_trimmed(
    client: httpx.AsyncClient, script: Script
) -> None:
    await client.post(
        "/v1/chat",
        json=_body(),
        headers={
            "X-Visitor-IP": "::ffff:203.0.113.7",
            "X-Visitor-User-Agent": "Mozilla/5.0 (test)",
            "X-Visitor-Referrer": "https://mihaylov.io/projects/threadline?utm_source=mail",
        },
    )

    [(_, _, visitor)] = script.started
    assert visitor is not None
    # The address as IPv4, keyed with this API's own salt. A digest never contains
    # the address, so checking for that would pass whatever was stored.
    assert visitor.ip_hash == hash_ip("203.0.113.7", SecretStr(SALT))
    assert visitor.user_agent == "Mozilla/5.0 (test)"
    assert visitor.referrer == "https://mihaylov.io/projects/threadline"


async def test_a_request_without_visitor_headers_is_still_answered(
    client: httpx.AsyncClient, script: Script
) -> None:
    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 200
    [(_, _, visitor)] = script.started
    assert visitor == ClientInfo(ip_hash=None, user_agent=None, referrer=None)


# --- failing ------------------------------------------------------------------


async def test_openai_down_before_the_answer_is_a_503(
    client: httpx.AsyncClient, script: Script
) -> None:
    script.events = [Classified("mihail_related")]
    script.error = AssistantError("OpenAI call failed (APIConnectionError): scripted failure")

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 503
    assert response.json() == {
        "detail": "The assistant is unavailable right now. Please try again in a moment."
    }
    assert "scripted" not in response.text


async def test_a_failure_mid_answer_ends_the_stream_with_an_error_event(
    client: httpx.AsyncClient, script: Script
) -> None:
    script.events = [Classified("mihail_related"), Token("Yes, ")]
    script.error = AssistantError("OpenAI reported the response failed (server_error: scripted)")

    response = await client.post("/v1/chat/stream", json=_body())

    assert response.status_code == 200, "the status line went out with the first event"
    events = _events(response.text)
    assert [name for name, _ in events] == ["route", "token", "error"]
    assert events[-1][1] == {
        "detail": "The assistant is unavailable right now. Please try again in a moment.",
        "status": 503,
    }
    assert "scripted" not in response.text


async def test_a_bug_mid_answer_is_a_500_error_event_that_says_nothing(
    client: httpx.AsyncClient, script: Script
) -> None:
    script.events = [Classified("small_talk")]
    script.error = KeyError("internal detail nobody outside should see")

    response = await client.post("/v1/chat/stream", json=_body())

    assert _events(response.text)[-1] == (
        "error",
        {"detail": "Something went wrong on our side.", "status": 500},
    )
    assert "internal detail" not in response.text


async def test_a_rejected_openai_key_is_a_500_not_a_503(
    client: httpx.AsyncClient, script: Script
) -> None:
    """A retry cannot fix configuration, so the status does not invite one."""
    script.events = [Classified("mihail_related")]
    script.error = ConfigError("OpenAI rejected the API key.")

    response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 500


async def test_a_bug_is_a_json_500_that_says_nothing(app: FastAPI, script: Script) -> None:
    """Not Starlette's plain-text fallback: every error body is JSON, so the proxy can
    read this one like the rest, and it carries the id to trace it by."""
    script.events = [Classified("small_talk")]
    script.error = KeyError("internal detail nobody outside should see")

    # The server raises a bug again after answering, so that its traceback is logged.
    # This transport would hand it to the test instead of the response.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://api",
        headers={"Authorization": f"Bearer {KEY}"},
    ) as client:
        response = await client.post("/v1/chat", json=_body())

    assert response.status_code == 500
    assert response.json() == {"detail": "Something went wrong on our side."}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


# --- tracing a request --------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, _body(), 200),
        ({"Authorization": "Bearer wrong-key-of-a-reasonable-length-here"}, _body(), 401),
        ({}, _body(""), 422),
    ],
    ids=["answered", "wrong-key", "invalid"],
)
async def test_every_response_carries_a_request_id(
    client: httpx.AsyncClient, headers: dict[str, str], body: dict[str, Any], status: int
) -> None:
    """The proxy writes this id into its own logs, so a line there can be matched
    with the lines here."""
    response = await client.post("/v1/chat", json=body, headers=headers)

    assert response.status_code == status
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_the_request_log_names_the_route_not_the_path(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path holds whatever the caller put in it. A proxy that let a visitor's words
    into a URL would otherwise put them in the log as well."""
    recorder = LogRecorder()
    monkeypatch.setattr(middleware, "log", recorder)

    rejected = await client.post(
        "/v1/messages/I am hiring at Acme/feedback", json={"session_id": SESSION, "rating": 1}
    )
    missing = await client.get("/no/such/route/Acme")

    assert (rejected.status_code, missing.status_code) == (422, 404)
    assert [line["route"] for line in recorder.lines] == [
        "/v1/messages/{message_id}/feedback",
        None,
    ]
    assert "Acme" not in repr(recorder.lines)


# --- leaving early ------------------------------------------------------------


async def _leave_after_the_first_event(app: FastAPI) -> dict[str, str]:
    """Ask for a stream as uvicorn would, and hang up once the first event arrives.

    httpx cannot hang up halfway, so this plays the server's part by hand: a scope
    shaped as uvicorn sends it (ASGI spec 2.3, which decides how Starlette notices a
    disconnect), the body, and then a disconnect. Returns the response's headers.
    """
    first_event_sent = asyncio.Event()
    body = json.dumps(_body()).encode()
    asked = False
    headers: dict[str, str] = {}

    async def receive() -> dict[str, Any]:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_event_sent.wait()
        return {"type": "http.disconnect"}

    # async although it awaits nothing: ASGI awaits whatever it is given as `send`.
    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
        if message["type"] == "http.response.start":
            headers.update((name.decode(), value.decode()) for name, value in message["headers"])
        if message["type"] == "http.response.body" and message.get("body"):
            first_event_sent.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/stream",
        "raw_path": b"/v1/chat/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"api"),
            (b"content-type", b"application/json"),
            (b"authorization", f"Bearer {KEY}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("api", 8000),
    }

    await app(scope, receive, send)  # returns once the visitor has gone
    return headers


async def test_a_visitor_who_leaves_does_not_stop_the_answer(app: FastAPI, script: Script) -> None:
    """The guarantee the whole turn registry exists for, through the real endpoint."""
    script.hold = asyncio.Event()

    await _leave_after_the_first_event(app)

    assert script.finished == [], "still held: the answer is not finished yet"
    turns: Turns = app.state.turns
    assert turns.busy(SESSION), "and still running without anyone reading it"

    script.hold.set()
    await turns.drain(grace_seconds=5)

    assert script.finished == ["Yes, he does."]


async def test_a_turn_keeps_its_request_id_after_the_visitor_has_gone(
    app: FastAPI, script: Script
) -> None:
    """A task copies the context it was created in, so what a turn logs after its
    request has ended still carries the id the proxy was given."""
    script.hold = asyncio.Event()

    headers = await _leave_after_the_first_event(app)
    # The request is over. Clearing this task's context as well leaves the turn's
    # own copy as the only place the id could still come from.
    structlog.contextvars.clear_contextvars()
    script.hold.set()
    await app.state.turns.drain(grace_seconds=5)

    assert script.request_ids == [headers["x-request-id"]]


async def test_a_silent_stream_is_kept_alive(
    client: httpx.AsyncClient, script: Script, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the model thinks, FastAPI sends a comment line so proxies do not give
    up on the connection. The interval lives in fastapi.routing, which keeps its own
    copy of the name -- patching fastapi.sse's does nothing."""
    monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.02)
    script.hold = asyncio.Event()
    asyncio.get_running_loop().call_later(0.2, script.hold.set)

    response = await client.post("/v1/chat/stream", json=_body())

    assert ": ping" in response.text
    assert _events(response.text)[-1][0] == "done"


# --- feedback -----------------------------------------------------------------


@pytest.fixture
def stored_feedback(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []

    async def record_feedback(**vote: Any) -> bool:
        await asyncio.sleep(0)
        if vote["session_id"] != SESSION or vote["message_id"] != MESSAGE_ID:
            return False
        stored.append(vote)
        return True

    monkeypatch.setattr(feedback_db, "record_feedback", record_feedback)
    return stored


async def test_a_rating_is_stored(
    client: httpx.AsyncClient, stored_feedback: list[dict[str, Any]]
) -> None:
    response = await client.post(
        f"/v1/messages/{MESSAGE_ID}/feedback",
        json={"session_id": SESSION, "rating": -1, "comment": "  Not what I asked.  "},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert stored_feedback == [
        {
            "session_id": SESSION,
            "message_id": MESSAGE_ID,
            "rating": -1,
            "comment": "Not what I asked.",
        }
    ]


async def test_a_blank_comment_is_no_comment(
    client: httpx.AsyncClient, stored_feedback: list[dict[str, Any]]
) -> None:
    await client.post(
        f"/v1/messages/{MESSAGE_ID}/feedback",
        json={"session_id": SESSION, "rating": 1, "comment": "   "},
    )

    assert stored_feedback[0]["comment"] is None


async def test_rating_an_answer_from_another_conversation_is_a_404(
    client: httpx.AsyncClient, stored_feedback: list[dict[str, Any]]
) -> None:
    response = await client.post(
        f"/v1/messages/{MESSAGE_ID}/feedback", json={"session_id": OTHER_SESSION, "rating": 1}
    )

    assert response.status_code == 404
    assert stored_feedback == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": 0}),
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": 2}),
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": "1"}),
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": True}),
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": 1, "comment": "x" * 1001}),
        ("/v1/messages/1/feedback", {"session_id": SESSION, "rating": 1, "comment": "a\x00b"}),
        ("/v1/messages/0/feedback", {"session_id": SESSION, "rating": 1}),
        (f"/v1/messages/{2**63}/feedback", {"session_id": SESSION, "rating": 1}),
        ("/v1/messages/1/feedback", {"session_id": "short", "rating": 1}),
    ],
    ids=[
        "zero",
        "two",
        "string",
        "boolean",
        "long-comment",
        "nul-comment",
        "id-zero",
        "id-past-bigint",
        "bad-session",
    ],
)
async def test_an_invalid_rating_is_a_422(
    client: httpx.AsyncClient,
    stored_feedback: list[dict[str, Any]],
    path: str,
    body: dict[str, Any],
) -> None:
    response = await client.post(path, json=body)

    assert response.status_code == 422
    assert stored_feedback == []


# --- starting and stopping ----------------------------------------------------


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record what the lifespan opens and closes, instead of doing it."""
    calls: list[str] = []

    async def get_pool() -> None:
        await asyncio.sleep(0)
        calls.append("pool opened")

    async def close_client() -> None:
        await asyncio.sleep(0)
        calls.append("client closed")

    async def close_pool() -> None:
        await asyncio.sleep(0)
        calls.append("pool closed")

    monkeypatch.setattr(main, "get_pool", get_pool)
    monkeypatch.setattr(main, "close_client", close_client)
    monkeypatch.setattr(main, "close_pool", close_pool)
    # The lifespan configures logging for the process; in a test that would take
    # over the test runner's own log capture.
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    return calls


@pytest.mark.parametrize("missing", ["PORTFOLIO_AI_API_KEY", "IP_HASH_SALT"])
async def test_the_api_will_not_start_without_its_secrets(
    app: FastAPI, lifecycle: list[str], monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    get_settings.cache_clear()

    with pytest.raises(ConfigError, match=missing):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover - never reached

    assert lifecycle == [], "refused before opening anything"


async def test_shutdown_lets_answers_finish_then_closes_what_they_use(
    app: FastAPI, client: httpx.AsyncClient, script: Script, lifecycle: list[str]
) -> None:
    script.hold = asyncio.Event()

    async with app.router.lifespan_context(app):
        assert lifecycle == ["pool opened"]
        request = asyncio.create_task(client.post("/v1/chat", json=_body()))
        await _until(lambda: len(script.started) == 1)
        asyncio.get_running_loop().call_later(0.05, script.hold.set)

    assert script.finished == ["Yes, he does."], "drained, not cancelled"
    assert lifecycle == ["pool opened", "client closed", "pool closed"]
    assert (await request).status_code == 200


def test_the_app_can_be_built_with_no_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing and building reads nothing: settings wait for the lifespan."""
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "PORTFOLIO_AI_API_KEY", "IP_HASH_SALT"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    assert isinstance(create_app().state.turns, Turns)
