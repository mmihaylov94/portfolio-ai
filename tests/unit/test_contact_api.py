"""``POST /v1/contact``, in process: who may call it, what it takes, and what it answers.

The flow behind it (``contact/service.handle``) is replaced by a stand-in that
records what it was handed, so these are about the endpoint alone: the key, the
shape of the body, the hourly limit, the statuses, and a caller that hangs up.
test_contact_service.py runs the flow itself.

Requests go through ``httpx.ASGITransport``, which calls the app directly in the
test's own event loop, as test_api.py's do.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from portfolio_ai.api import main, schemas
from portfolio_ai.api.main import create_app
from portfolio_ai.api.turns import Turns
from portfolio_ai.config import get_settings
from portfolio_ai.contact import service
from portfolio_ai.contact.desk import Desk
from portfolio_ai.contact.replies import Replies
from portfolio_ai.contact.service import Outcome
from portfolio_ai.exceptions import ConfigError, MailError

KEY = "unit-tests-api-key-that-is-long-enough"
SALT = "unit-tests-ip-hashing-key-long-enough"

BODY = {
    "email": "visitor@example.com",
    "subject": "A tool for our warehouse",
    "message": "We would like a small web tool for our team.",
    "reason": "project",
}
OUTCOME = Outcome(status="accepted", acknowledged=True, spam_score=0, cost=Decimal("0.0005"))


@dataclass
class Handled:
    """Stands in for the flow: what it was handed, and what it should do."""

    calls: list[dict[str, str]] = field(default_factory=list)
    # The count of replies each call was handed.
    replies: list[Replies] = field(default_factory=list)
    error: Exception | None = None
    # Set to hold every call until it is released, to catch one in mid-flight.
    hold: asyncio.Event | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: int = 0


@pytest.fixture
def handled(monkeypatch: pytest.MonkeyPatch) -> Handled:
    record = Handled()

    async def handle(*, replies: Replies, **fields: str) -> Outcome:
        record.calls.append(fields)
        record.replies.append(replies)
        record.started.set()
        if record.hold is not None:
            await record.hold.wait()
        if record.error is not None:
            raise record.error
        record.finished += 1
        return OUTCOME

    monkeypatch.setattr(service, "handle", handle)
    return record


@pytest.fixture(autouse=True)
def _api_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORTFOLIO_AI_API_KEY", KEY)
    monkeypatch.setenv("IP_HASH_SALT", SALT)
    get_settings.cache_clear()


@pytest.fixture
async def app(handled: Handled) -> AsyncIterator[FastAPI]:
    """A fresh app per test, so no test inherits another's hourly count."""
    application = create_app()
    yield application
    if handled.hold is not None:
        handled.hold.set()
    await application.state.contact.drain(grace_seconds=5)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://api",
        headers={"Authorization": f"Bearer {KEY}"},
    ) as test_client:
        yield test_client


# --- a message that is taken -----------------------------------------------------------


async def test_a_message_is_handed_to_the_flow_and_answered_once_it_is_dealt_with(
    client: httpx.AsyncClient, handled: Handled
) -> None:
    response = await client.post("/v1/contact", json=BODY)

    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    assert handled.calls == [BODY]


async def test_the_answer_says_nothing_about_what_was_decided(
    client: httpx.AsyncClient, handled: Handled
) -> None:
    """Blocked as spam or passed on, the caller hears the same thing: the owner has
    been told. A visitor is not informed that they were taken for spam, and nor is
    whoever is probing the form to learn what gets through."""
    response = await client.post("/v1/contact", json=BODY)

    assert handled.finished == 1
    assert set(response.json()) == {"status"}


async def test_the_flow_is_handed_this_apps_own_count_of_replies(
    app: FastAPI, client: httpx.AsyncClient, handled: Handled
) -> None:
    """One count per app, which is one per process in production: the same object
    for every message, so that a second message to one inbox is seen as one."""
    await client.post("/v1/contact", json=BODY)
    await client.post("/v1/contact", json={**BODY, "email": "other@example.com"})

    assert len(handled.replies) == 2
    assert all(given is app.state.contact.replies for given in handled.replies)
    assert create_app().state.contact.replies is not app.state.contact.replies


async def test_the_fields_arrive_trimmed(client: httpx.AsyncClient, handled: Handled) -> None:
    padded = {name: f"  {value}\n" for name, value in BODY.items()}

    assert (await client.post("/v1/contact", json=padded)).status_code == 200
    assert handled.calls == [BODY]


# --- who may call, and with what ----------------------------------------------------------


async def test_without_the_key_nothing_is_taken(app: FastAPI, handled: Handled) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as anonymous:
        missing = await anonymous.post("/v1/contact", json=BODY)
        wrong = await anonymous.post(
            "/v1/contact", json=BODY, headers={"Authorization": "Bearer not-the-key"}
        )

    assert (missing.status_code, wrong.status_code) == (401, 401)
    assert handled.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {name: value for name, value in BODY.items() if name != "message"},
        {**BODY, "message": "   "},
        {**BODY, "message": 5},
        {**BODY, "phone": "0123456789"},
        {**BODY, "message": "x" * (schemas.MAX_CONTACT_MESSAGE + 1)},
        {**BODY, "subject": "x" * (schemas.MAX_CONTACT_SUBJECT + 1)},
        {**BODY, "email": "x" * (schemas.MAX_CONTACT_EMAIL + 1)},
        {**BODY, "reason": "x" * (schemas.MAX_CONTACT_REASON + 1)},
        {**BODY, "message": "Hello" + chr(0) + "there"},
        {**BODY, "subject": "Hello" + chr(27) + "[2J"},
        # Every field has every rule: one case each for the three the cases above
        # leave out.
        {**BODY, "email": "visitor" + chr(0) + "@example.com"},
        {**BODY, "reason": "project" + chr(127)},
        {**BODY, "email": "  "},
        {**BODY, "subject": ""},
        {**BODY, "reason": " \t "},
    ],
    ids=[
        "a-field-missing",
        "a-field-of-spaces",
        "not-text",
        "a-field-too-many",
        "message-too-long",
        "subject-too-long",
        "email-too-long",
        "reason-too-long",
        "a-nul",
        "an-escape-sequence",
        "a-nul-in-the-address",
        "a-delete-in-the-reason",
        "an-address-of-spaces",
        "no-subject",
        "a-reason-of-spaces",
    ],
)
async def test_a_body_that_does_not_fit_is_refused_and_nothing_is_taken(
    client: httpx.AsyncClient, handled: Handled, body: dict[str, Any]
) -> None:
    response = await client.post("/v1/contact", json=body)

    assert response.status_code == 422
    assert handled.calls == []


async def test_each_field_may_be_exactly_as_long_as_its_limit(
    client: httpx.AsyncClient, handled: Handled
) -> None:
    longest = {
        "email": "x" * schemas.MAX_CONTACT_EMAIL,
        "subject": "x" * schemas.MAX_CONTACT_SUBJECT,
        "message": "x" * schemas.MAX_CONTACT_MESSAGE,
        "reason": "x" * schemas.MAX_CONTACT_REASON,
    }

    assert (await client.post("/v1/contact", json=longest)).status_code == 200
    assert handled.calls == [longest]


def test_the_four_limits_are_the_numbers_the_sites_api_checks_first() -> None:
    """Stated twice on purpose: here, and as ``MAX_CHARS`` in the portfolio's
    api/src/contact.js, with the form's ``maxlength`` beside it. The tests above
    measure against the constants, so they pass whatever the constants are; this is
    the one that notices a limit moving on this side alone."""
    assert (
        schemas.MAX_CONTACT_EMAIL,
        schemas.MAX_CONTACT_SUBJECT,
        schemas.MAX_CONTACT_MESSAGE,
        schemas.MAX_CONTACT_REASON,
    ) == (254, 300, 5000, 40)


async def test_whether_a_reason_or_an_address_is_real_is_not_this_endpoints_question(
    client: httpx.AsyncClient, handled: Handled
) -> None:
    """Shape and size are checked here. A reason the form does not offer, or an
    address that is not one, is the rules' to find, and their answer is an email to
    the owner saying a submission was blocked, not a refusal to the caller."""
    odd = {**BODY, "reason": "sales", "email": "not-an-address"}

    assert (await client.post("/v1/contact", json=odd)).status_code == 200
    assert handled.calls == [odd]


# --- the limit ----------------------------------------------------------------------------


async def test_the_hours_limit_is_for_the_whole_service_and_says_when_to_come_back(
    client: httpx.AsyncClient, handled: Handled, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTACT_MAX_PER_HOUR", "2")
    get_settings.cache_clear()

    first = await client.post("/v1/contact", json=BODY)
    second = await client.post("/v1/contact", json={**BODY, "email": "other@example.com"})
    third = await client.post("/v1/contact", json={**BODY, "email": "third@example.com"})

    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    assert 3590 <= int(third.headers["retry-after"]) <= 3600
    assert len(handled.calls) == 2, "the third was never handed to the flow"


# --- when the message does not get through ---------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MailError("The contact form cannot send. Not set: CONTACT_TO_EMAIL."), 503),
        (MailError("The email could not be sent (ConnectionRefusedError)."), 503),
        (ConfigError("The mail server rejected the login (code 535)."), 500),
    ],
    ids=["not-set-up", "send-failed", "login-refused"],
)
async def test_a_message_the_owner_could_not_be_told_of_is_an_error(
    client: httpx.AsyncClient, handled: Handled, error: Exception, status: int
) -> None:
    """The site then tells the visitor the message was not sent. The sentence that
    goes back names no setting and repeats nothing the mail server said."""
    handled.error = error

    response = await client.post("/v1/contact", json=BODY)

    assert response.status_code == status
    for private in ("CONTACT_TO_EMAIL", "ConnectionRefusedError", "535", "login"):
        assert private not in response.text


async def test_while_the_service_is_stopping_a_message_is_turned_away(
    app: FastAPI, client: httpx.AsyncClient, handled: Handled
) -> None:
    app.state.contact.closing = True

    response = await client.post("/v1/contact", json=BODY)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert handled.calls == []


# --- a caller that gives up -------------------------------------------------------------------


async def test_a_caller_that_hangs_up_does_not_stop_a_message_in_hand(
    app: FastAPI, client: httpx.AsyncClient, handled: Handled
) -> None:
    """The flow may be between the visitor's acknowledgement and the owner's
    notification. Cancelling the request cancels the waiting; the message in hand is
    seen through."""
    handled.hold = asyncio.Event()
    request = asyncio.create_task(client.post("/v1/contact", json=BODY))
    await handled.started.wait()

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert handled.finished == 0
    assert app.state.contact.in_hand == 1, "still being dealt with"
    handled.hold.set()
    await app.state.contact.drain(grace_seconds=5)
    assert handled.finished == 1


# --- stopping -----------------------------------------------------------------------------------


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch, handled: Handled) -> list[str]:
    """Record when the lifespan opens and closes things, instead of doing it."""
    calls: list[str] = []

    def recorder(what: str) -> Callable[[], Awaitable[None]]:
        async def record() -> None:
            await asyncio.sleep(0)
            calls.append(f"{what}, {handled.finished} dealt with")

        return record

    monkeypatch.setattr(main, "get_pool", recorder("pool opened"))
    monkeypatch.setattr(main, "close_client", recorder("client closed"))
    monkeypatch.setattr(main, "close_pool", recorder("pool closed"))
    # The lifespan configures logging for the process; in a test that would take
    # over the test runner's own log capture.
    monkeypatch.setattr(main, "configure_logging", lambda: None)
    return calls


async def test_shutdown_sees_a_message_in_hand_through_before_closing_what_it_uses(
    app: FastAPI, client: httpx.AsyncClient, handled: Handled, lifecycle: list[str]
) -> None:
    handled.hold = asyncio.Event()

    async with app.router.lifespan_context(app):
        request = asyncio.create_task(client.post("/v1/contact", json=BODY))
        await handled.started.wait()
        asyncio.get_running_loop().call_later(0.05, handled.hold.set)

    assert lifecycle == [
        "pool opened, 0 dealt with",
        "client closed, 1 dealt with",
        "pool closed, 1 dealt with",
    ]
    assert (await request).status_code == 200
    # And nothing new is taken once the stopping has begun: the flow's stand-in would
    # answer this one at once if it were let through.
    late = await client.post("/v1/contact", json=BODY)
    assert late.status_code == 503
    assert len(handled.calls) == 1


async def test_shutdown_waits_for_answers_and_messages_at_once_not_in_turn(
    app: FastAPI, lifecycle: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One grace for both. Docker gives the container 45 seconds to stop, the server
    spends up to 30 of them on open requests, and the grace is 10: waited for one
    after the other, the two could take 20 and the container would be killed with a
    message half dealt with."""
    order: list[str] = []

    def waiting(name: str) -> Callable[[object, float], Awaitable[None]]:
        # Named as the real ones are: this test's own app is drained by keyword
        # when the test is over.
        async def drain(
            self: object,  # ruff: ignore[unused-function-argument]
            grace_seconds: float,  # ruff: ignore[unused-function-argument]
        ) -> None:
            order.append(f"{name} begun")
            await asyncio.sleep(0)
            order.append(f"{name} over")

        return drain

    monkeypatch.setattr(Turns, "drain", waiting("answers"))
    monkeypatch.setattr(Desk, "drain", waiting("messages"))

    async with app.router.lifespan_context(app):
        pass

    assert order[:2] == ["answers begun", "messages begun"], "both begun before either is over"
    assert lifecycle[-2:] == ["client closed, 0 dealt with", "pool closed, 0 dealt with"]
