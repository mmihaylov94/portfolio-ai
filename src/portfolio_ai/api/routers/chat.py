"""``POST /v1/chat`` and ``POST /v1/chat/stream``: the same answer, two ways.

Both take the same body and pass through the same admission (``deps.admit_turn``),
which does every check and starts the turn. What is left for the endpoints is
reading the turn's events and choosing a format:

- ``/v1/chat`` waits for the end and returns one JSON object. Simple to call from
  curl, and what a client that cannot read a stream uses.
- ``/v1/chat/stream`` sends each event as it happens, as server-sent events, which
  is what makes words appear while the rest of the answer is still being written.

Server-sent events are plain text over an ordinary HTTP response that stays open:
``event:`` names the event, ``data:`` carries it, and a blank line ends it. FastAPI
0.141 writes that format itself when an endpoint is an async generator declared with
``response_class=EventSourceResponse`` -- each ``yield`` becomes one event -- and
sends a ``: ping`` comment after fifteen seconds of silence, which keeps proxies
from giving up on a connection while the model thinks.

A stream is these events, in this order::

    route    {"classification": "mihail_related"}
    search   {}                                     once per search
    token    {"text": "Yes, "}                      many
    done     {"message_id": 4812, "classification": ..., "citations": [...], "usage": {...}}

or, if the answer fails after the stream has begun, ``error`` in place of ``done``.
Every stream ends with exactly one of the two.
"""

from collections.abc import AsyncIterator
from typing import Annotated, assert_never

from fastapi import APIRouter, Depends
from fastapi.sse import EventSourceResponse, ServerSentEvent

from portfolio_ai.api.deps import Refusal, admit_turn
from portfolio_ai.api.errors import public_error
from portfolio_ai.api.schemas import ChatResponse, DoneData, ErrorData, RouteData, TokenData
from portfolio_ai.api.security import require_api_key
from portfolio_ai.api.turns import Turn
from portfolio_ai.assistant.agent import Classified, Done, Event, Searched, Token

# dependencies= on the router applies require_api_key to every route in it, ahead of
# the route's own dependency. One thing happens earlier still: FastAPI reads the body
# and parses the JSON before any dependency runs, so a body that is not JSON at all
# is a 422 even without a key.
router = APIRouter(prefix="/v1", tags=["chat"], dependencies=[Depends(require_api_key)])

# The endpoints take this and nothing else. See deps.py for why that is load-bearing.
Admission = Annotated[Turn | Refusal, Depends(admit_turn)]


@router.post("/chat")
async def chat(admission: Admission) -> ChatResponse:
    """Answer a message, and return the whole answer when it is finished."""
    if isinstance(admission, Refusal):
        return ChatResponse.refused(admission.session_id, admission.reply)

    # A failed turn raises out of events(), and errors.py turns it into a status.
    async for event in admission.events():
        if isinstance(event, Done):
            return ChatResponse.answered(admission.session_id, event.result)

    raise AssertionError("a turn ends in Done or raises")  # pragma: no cover


def _to_sse(event: Event) -> ServerSentEvent:
    """One of the assistant's events as the event a client receives."""
    match event:
        case Classified(label):
            return ServerSentEvent(event="route", data=RouteData(classification=label))
        case Searched():
            # Empty on purpose. The query is text the model wrote, and it never goes
            # through the link filter that every word of the answer does -- so it
            # could carry the very URLs the answer is not allowed to. "A search ran"
            # is all a chat window needs to say it is looking something up.
            return ServerSentEvent(event="search", data={})
        case Token(text):
            return ServerSentEvent(event="token", data=TokenData(text=text))
        case Done(result):
            return ServerSentEvent(event="done", data=DoneData.answered(result))
        case _:
            # mypy checks this line is unreachable: add a fifth kind of event and
            # forget it here, and the type check fails rather than the stream.
            assert_never(event)


@router.post("/chat/stream", response_class=EventSourceResponse)
async def chat_stream(admission: Admission) -> AsyncIterator[ServerSentEvent]:
    """Answer a message as server-sent events, while it is being written.

    **This generator must never raise.** Once it has started, the 200 and the
    headers are already on their way; an exception from here would reach the
    client as a response that stops mid-sentence with no explanation. So a failed
    answer is reported as an ``error`` event and the stream ends normally.

    Only ``Exception`` is caught. Cancellation -- the visitor closing the page -- is
    a ``BaseException`` and passes straight through, which is right: it stops this
    reader, and the turn itself carries on (turns.py).
    """
    if isinstance(admission, Refusal):
        yield ServerSentEvent(event="token", data=TokenData(text=admission.reply))
        yield ServerSentEvent(event="done", data=DoneData.refused())
        return

    try:
        async for event in admission.events():
            yield _to_sse(event)
    except Exception as exc:
        error = public_error(exc)
        yield ServerSentEvent(
            event="error", data=ErrorData(detail=error.detail, status=error.status)
        )
