"""Deciding whether a message gets an answer, before a single byte of one is sent.

``admit_turn`` is a FastAPI *dependency*: a function named in an endpoint's
parameters with ``Depends(...)``, which FastAPI calls before the endpoint and whose
return value it passes in. FastAPI resolves them per request, fills in their own
parameters the same way -- the request body, headers, other dependencies -- and
runs all of it before the endpoint's body starts.

That ordering is why every check lives here rather than in the endpoints. The
streaming endpoint's response begins the moment its body starts: from then on the
status line is 200 whatever happens. A refusal raised here is still an ordinary 409
or 429; raised in the endpoint, it would be a stream that says "OK" and then stops.

Two details that are easy to undo by accident:

- **This takes the request body itself**, and the endpoints take nothing but this.
  FastAPI validates an endpoint's own parameters *after* running its dependencies,
  so if the body were the endpoint's parameter, a malformed one would be rejected
  with a 422 only after an answer had been started here -- and nobody would be
  listening for it. A test checks the endpoints' signatures for exactly this.
- **Nothing awaits after the database reads.** The checks and the start happen in
  one uninterrupted stretch, so two requests for the same conversation cannot both
  pass the "already answering?" check. See ``turns.py``.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, Request, status

from portfolio_ai.api.schemas import ChatRequest
from portfolio_ai.api.security import visitor
from portfolio_ai.api.turns import Turn, Turns
from portfolio_ai.assistant import conversation
from portfolio_ai.assistant.prompts.loader import DAILY_LIMIT_REPLY
from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.chat import ClientInfo

log = structlog.get_logger(__name__)

# The spending limit is a rolling window rather than a calendar day: no timezone to
# pick, and no midnight at which the whole budget becomes available in one go.
SPEND_WINDOW = dt.timedelta(hours=24)

# How long to tell a caller to wait when the service is busy or restarting. Short:
# both conditions pass in seconds.
RETRY_SOON_SECONDS = 5


@dataclass(frozen=True)
class Refusal:
    """The day's spending limit is reached: a fixed reply instead of an answer.

    Not an error, on purpose. A visitor should read a polite sentence in the chat
    window, not a failure -- and the endpoints deliver it exactly like an answer,
    minus a message id, since nothing is stored and there is nothing to rate.
    """

    session_id: str
    reply: str


def turns_of(request: Request) -> Turns:
    """The app's registry of running turns.

    ``app.state`` is a bag of attributes Starlette provides for exactly this, and it
    is untyped -- reading from it gives ``Any``. The ``isinstance`` check turns that
    back into something mypy can follow, and fails clearly if the app was built
    without one.
    """
    turns = request.app.state.turns
    if not isinstance(turns, Turns):  # pragma: no cover - create_app always sets it
        raise TypeError("app.state.turns is not a Turns registry; build the app with create_app()")
    return turns


async def admit_turn(
    request: Request,
    body: ChatRequest,
    client: Annotated[ClientInfo, Depends(visitor)],
) -> Turn | Refusal:
    """Start an answer to ``body``, or say why not.

    In order, cheapest refusal first once the reads are done:

    ====================================  =================================
    shutting down                         503, Retry-After
    this conversation is mid-answer       409
    too many questions in the window      429, Retry-After
    the day's spending limit reached      the fixed reply (not an error)
    too many answers in flight overall    503, Retry-After
    ====================================  =================================

    The spending check comes before the capacity one because a refusal takes no
    slot: when both are true, the visitor is better off with the sentence than with
    "try again in five seconds", which would only produce the sentence.
    """
    settings = get_settings()
    turns = turns_of(request)

    # Both reads happen first, before any check. A database that is down fails here,
    # as a 503 from errors.py, with nothing started.
    recent = await chat_db.recent_questions(
        body.session_id,
        window=dt.timedelta(minutes=settings.session_rate_window_minutes),
        limit=settings.session_rate_limit,
    )
    spent = await chat_db.spend_since(SPEND_WINDOW)

    # --- No await below this line. See the module docstring. ---

    if turns.closing:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant is restarting. Please try again in a moment.",
            headers={"Retry-After": str(RETRY_SOON_SECONDS)},
        )

    if turns.busy(body.session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A reply in this conversation is still being written.",
        )

    if recent.count >= settings.session_rate_limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="That is a lot of questions in a short time. Please wait a little.",
            headers={"Retry-After": str(recent.retry_after)},
        )

    if spent >= settings.daily_spend_cap_usd:
        # Numbers only, as everywhere: which conversation asked is not needed to
        # know the limit was hit.
        log.warning(
            "daily_spend_cap_reached",
            spent_usd=str(spent),
            cap_usd=str(settings.daily_spend_cap_usd),
        )
        return Refusal(session_id=body.session_id, reply=DAILY_LIMIT_REPLY.text)

    if turns.in_flight >= settings.max_concurrent_turns:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The assistant is busy. Please try again in a moment.",
            headers={"Retry-After": str(RETRY_SOON_SECONDS)},
        )

    # conversation.chat(...) builds an async generator and runs none of it; the turn's
    # task is what runs it. Looked up through the module rather than imported by
    # name, so a test can replace it.
    return turns.start(
        body.session_id, conversation.chat(body.session_id, body.message, client=client)
    )
