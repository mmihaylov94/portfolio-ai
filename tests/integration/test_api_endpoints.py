"""The API end to end: real app, real database, real assistant -- only OpenAI is fake.

The unit tests in tests/unit/test_api.py pin down each check with everything around
it replaced. These make sure the pieces meet: that a stored answer's id is the one
the browser gets back, that the limits count rows the API itself wrote, that a
visitor's address reaches the database only as a hash, and that nothing a visitor
typed ends up in a log.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, MutableMapping
from typing import Any

import corpus
import httpx
import pytest
from fastapi import FastAPI
from openai_fake import FakeOpenAI, install_fake_openai
from pydantic import SecretStr

from portfolio_ai.api.main import create_app
from portfolio_ai.api.security import hash_ip
from portfolio_ai.config import get_settings
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.db.pool import get_pool
from portfolio_ai.logging import configure_logging

pytestmark = pytest.mark.integration

KEY = "integration-tests-api-key-long-enough"
SALT = "integration-tests-hashing-key-long-enough"
SESSION = "cccccccc-1111-4222-8333-444444444444"
OTHER_SESSION = "dddddddd-1111-4222-8333-444444444444"
VISITOR = {
    "X-Visitor-IP": "198.51.100.23",
    "X-Visitor-User-Agent": "Mozilla/5.0 (integration test)",
    "X-Visitor-Referrer": "https://mihaylov.io/?ref=newsletter",
}


@pytest.fixture(autouse=True)
async def _setup(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("PORTFOLIO_AI_API_KEY", KEY)
    monkeypatch.setenv("IP_HASH_SALT", SALT)
    # The limits at their defaults, whatever the developer's .env says: a local
    # DAILY_SPEND_CAP_USD=0, set to try the refusal, would otherwise fail these
    # tests for a reason that has nothing to do with the code.
    monkeypatch.setenv("SESSION_RATE_LIMIT", "20")
    monkeypatch.setenv("SESSION_RATE_WINDOW_MINUTES", "15")
    monkeypatch.setenv("MAX_CONCURRENT_TURNS", "10")
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "1.00")
    get_settings.cache_clear()
    await corpus.clear()
    await corpus.seed(
        "tech-stack",
        "Tech Stack",
        [("Does Mihail work with Laravel?", "Yes, it is his main PHP framework.", corpus.axis(0))],
        url="https://mihaylov.io/#about",
    )
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    yield application
    # Before the pool is closed after the test: a turn still running would find it gone.
    await application.state.turns.drain(grace_seconds=5)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api",
        headers={"Authorization": f"Bearer {KEY}", **VISITOR},
    ) as test_client:
        yield test_client


def _small_talk(fake: FakeOpenAI, reply: str = "Hi! How can I help?") -> None:
    fake.classify("small_talk")
    fake.say(reply)


def _events(stream: str) -> list[tuple[str, Any]]:
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


def _from_the_test_client(raw: str) -> bool:
    """Whether a log line is httpx reporting a request the test itself made.

    httpx logs every URL its client requests, at INFO. The test's client is the
    harness, not the API -- in production the caller is the Express API, in another
    container -- so what it logs about its own requests says nothing about this
    process. The OpenAI SDK's lines come from httpx too, and are kept: their URLs
    point at OpenAI, not at http://api.
    """
    if not raw.startswith("{"):
        return False
    line = json.loads(raw)
    event = str(line.get("event", ""))
    return (
        line.get("logger") == "httpx"
        and event.startswith("HTTP Request: ")
        and " http://api/" in event
    )


async def _rows(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return [dict(row) for row in await cur.fetchall()]


async def test_an_answer_is_stored_with_where_it_came_from(
    client: httpx.AsyncClient, fake: FakeOpenAI
) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")

    response = await client.post(
        "/v1/chat", json={"session_id": SESSION, "message": "Does Mihail work with Laravel?"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Yes, Laravel is his main PHP framework."
    assert body["citations"][0]["doc_id"] == "tech-stack"

    messages = await _rows("select id, role, cost_usd from chat_messages order by id")
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert body["message_id"] == messages[1]["id"], "the id the browser rates is the stored answer"

    [session] = await _rows("select client_ip_hash, user_agent, referrer from chat_sessions")
    assert session["client_ip_hash"] == hash_ip("198.51.100.23", SecretStr(SALT))
    assert session["user_agent"] == "Mozilla/5.0 (integration test)"
    assert session["referrer"] == "https://mihaylov.io/"


async def test_the_stream_ends_with_the_stored_answers_id(
    client: httpx.AsyncClient, fake: FakeOpenAI
) -> None:
    _small_talk(fake)

    response = await client.post("/v1/chat/stream", json={"session_id": SESSION, "message": "Hi"})

    events = _events(response.text)
    assert events[0][0] == "route"
    assert events[-1][0] == "done"
    [answer] = await _rows("select id, content from chat_messages where role = 'assistant'")
    assert events[-1][1]["message_id"] == answer["id"]
    assert "".join(data["text"] for name, data in events if name == "token") == answer["content"]


async def test_a_stored_answer_can_be_rated_by_its_own_conversation_only(
    client: httpx.AsyncClient, fake: FakeOpenAI
) -> None:
    _small_talk(fake)
    answered = await client.post("/v1/chat", json={"session_id": SESSION, "message": "Hi"})
    message_id = answered.json()["message_id"]

    stranger = await client.post(
        f"/v1/messages/{message_id}/feedback", json={"session_id": OTHER_SESSION, "rating": -1}
    )
    owner = await client.post(
        f"/v1/messages/{message_id}/feedback",
        json={"session_id": SESSION, "rating": -1, "comment": "Too short."},
    )

    assert stranger.status_code == 404
    assert owner.status_code == 204
    assert await _rows("select message_id, rating, comment from message_feedback") == [
        {"message_id": message_id, "rating": -1, "comment": "Too short."}
    ]


async def test_the_limit_counts_the_questions_the_api_stored(
    client: httpx.AsyncClient, fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SESSION_RATE_LIMIT", "2")
    get_settings.cache_clear()

    statuses = []
    for number in range(3):
        _small_talk(fake)
        response = await client.post(
            "/v1/chat", json={"session_id": SESSION, "message": f"Hello {number}"}
        )
        statuses.append(response.status_code)

    assert statuses == [200, 200, 429]
    assert fake.pending == 2, "the third turn never called OpenAI"


async def test_the_spending_limit_reads_what_answers_actually_cost(
    client: httpx.AsyncClient, fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One small-talk turn through the fake costs about $0.00013 (two calls at the
    # fake's token counts), so this cap lets exactly one through.
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "0.0001")
    get_settings.cache_clear()

    _small_talk(fake)
    first = await client.post("/v1/chat", json={"session_id": SESSION, "message": "Hi"})
    second = await client.post("/v1/chat", json={"session_id": OTHER_SESSION, "message": "Hi"})

    assert first.json()["message_id"] is not None
    assert second.status_code == 200
    assert second.json()["message_id"] is None, "refused: nothing stored, nothing to rate"
    assert len(await _rows("select id from chat_messages")) == 2, "only the first turn"


async def test_a_message_postgres_cannot_store_is_refused_before_it_is_paid_for(
    client: httpx.AsyncClient, fake: FakeOpenAI
) -> None:
    response = await client.post(
        "/v1/chat", json={"session_id": SESSION, "message": "nul \u0000 byte"}
    )

    assert response.status_code == 422
    assert fake.requests == []
    assert await _rows("select id from chat_messages") == []


async def test_an_answer_the_visitor_left_is_still_stored(
    app: FastAPI, fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disconnect after the first event, with the turn held mid-search: the request
    ends, the turn does not, and the whole exchange is in the database afterwards."""
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")

    searching = asyncio.Event()
    release = asyncio.Event()
    real_search = docs_db.search_chunks

    async def held_search(vector: list[float], *, top_k: int) -> list[RetrievedChunk]:
        searching.set()
        await release.wait()
        return await real_search(vector, top_k=top_k)

    monkeypatch.setattr(docs_db, "search_chunks", held_search)

    first_event_sent = asyncio.Event()
    body = json.dumps({"session_id": SESSION, "message": "Does Mihail work with Laravel?"})
    asked = False

    async def receive() -> dict[str, Any]:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": body.encode(), "more_body": False}
        await first_event_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:  # ruff: ignore[unused-async]
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

    await app(scope, receive, send)
    await searching.wait()
    assert await _rows("select id from chat_messages") == [], "the visitor left mid-answer"

    release.set()
    await app.state.turns.drain(grace_seconds=5)

    stored = await _rows("select role, content from chat_messages order by id")
    assert stored == [
        {"role": "user", "content": "Does Mihail work with Laravel?"},
        {"role": "assistant", "content": "Yes, Laravel is his main PHP framework."},
    ]


async def test_nothing_a_visitor_types_reaches_the_logs(
    client: httpx.AsyncClient,
    fake: FakeOpenAI,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The privacy canary. Every line the process writes, as production writes it,
    through a stored answer, a stream, a rating, a malformed URL and a failure.

    Logging is configured for real, at debug, so the lines are the JSON production
    writes; the root logger's handlers are put back afterwards, or pytest would lose
    its own log capture for every test after this one.
    """
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    configure_logging("DEBUG")
    try:
        question = "My name is Canary McTestface and I am hiring"
        comment = "Canary comment with a phone number 07700 900123"
        fake.classify("mihail_related")
        fake.search("hiring")
        fake.say("Canary answer text about hiring.")
        answered = await client.post("/v1/chat", json={"session_id": SESSION, "message": question})
        _small_talk(fake, reply="Canary streamed reply.")
        await client.post("/v1/chat/stream", json={"session_id": SESSION, "message": question})
        await client.post(
            f"/v1/messages/{answered.json()['message_id']}/feedback",
            json={"session_id": SESSION, "rating": -1, "comment": comment},
        )
        # A visitor's words where a message id belongs, as a careless proxy might send.
        await client.post(
            f"/v1/messages/{question}/feedback", json={"session_id": SESSION, "rating": 1}
        )
        fake.http_error(500)
        await client.post("/v1/chat", json={"session_id": OTHER_SESSION, "message": question})
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)

    captured = capfd.readouterr().out
    logged = "\n".join(raw for raw in captured.splitlines() if not _from_the_test_client(raw))
    lines = [json.loads(line) for line in logged.splitlines() if line.startswith("{")]
    assert any(line["event"] == "request" for line in lines), "the logs were captured"
    assert any(line["event"] == "turn_answered" for line in lines)
    assert "Canary" not in logged
    assert "07700" not in logged
    assert "198.51.100.23" not in logged, "nor the visitor's address"
