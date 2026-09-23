"""The shapes that cross the HTTP boundary, in and out.

Pydantic models, per the project rule that anything crossing a boundary is typed.
FastAPI reads them twice: to validate a request before any of our code runs -- a
body that does not fit is a 422 and the endpoint is never called -- and to write
the OpenAPI schema at ``/docs``.

The assistant core has its own dataclasses for the same ideas (``TurnResult``,
``Citation``). These are deliberately separate: those describe everything the
assistant knows about an answer, cost included, and these describe what a caller
is allowed to see. Converting one into the other is where that line is drawn.
"""

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, StringConstraints

from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.assistant.classifier import Classification

# The longest message accepted. A question to a portfolio's assistant is a sentence
# or three; two thousand characters is room for a pasted job description and still
# a bound on what one message can cost.
MAX_MESSAGE_CHARS = 2000
MAX_COMMENT_CHARS = 1000

# C0 control characters and DEL, except tab, line feed and carriage return. NUL is
# the one that matters: Postgres text cannot hold it, so a message containing one
# would be answered -- and paid for -- and then fail to store, leaving a turn that
# neither the rate limit nor the spending limit ever sees. The rest go because no
# keyboard produces them.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _no_control_characters(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("must not contain control characters")
    return value


def _not_a_boolean(value: object) -> object:
    # In Python, True == 1 -- bool is a subclass of int -- so a Literal[-1, 1] check
    # accepts JSON `true` as a thumbs up, while `false` (0) fails. Refused outright
    # rather than half-supported.
    #
    # ValueError although this is a type check, which ruff would rather see as a
    # TypeError: Pydantic turns a ValueError raised in a validator into a 422, and
    # lets a TypeError escape as a crash.
    if isinstance(value, bool):
        raise ValueError(  # ruff: ignore[type-check-without-type-error]
            "must be 1 or -1, not true or false"
        )
    return value


# Annotated types: the base type first, then whatever should run on it, in order.
# Pydantic reads the extra arguments; to everything else these are plain `str`.
# StringConstraints strips and measures, then the validator checks what is left.
SessionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{8,100}$")]
Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS),
    AfterValidator(_no_control_characters),
]
Comment = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=MAX_COMMENT_CHARS),
    AfterValidator(_no_control_characters),
]


class ChatRequest(BaseModel):
    """A visitor's message. The same body for the plain and the streaming endpoint."""

    # An unknown field is an error rather than ignored: the caller is our own proxy,
    # and a misspelt `sesion_id` should fail loudly in development, not start a
    # conversation with no session.
    model_config = ConfigDict(extra="forbid")

    # Minted by the browser (crypto.randomUUID()) and kept in localStorage. Opaque
    # to the API; the pattern only keeps it to something that is safe to index.
    session_id: SessionId
    message: Message


class CitationOut(BaseModel):
    """A document an answer drew on, for the chat UI to link under the reply."""

    doc_id: str
    title: str
    url: str | None
    section: str | None


class Usage(BaseModel):
    """What an answer took, minus what it cost -- the bill is not the visitor's business."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    first_token_ms: int | None


def _citations(result: TurnResult) -> list[CitationOut]:
    return [
        CitationOut(doc_id=c.doc_id, title=c.title, url=c.url, section=c.section)
        for c in result.citations
    ]


def _usage(result: TurnResult) -> Usage:
    return Usage(
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
        first_token_ms=result.first_token_ms,
    )


class ChatResponse(BaseModel):
    """The whole answer at once, from ``POST /v1/chat``."""

    reply: str
    session_id: str
    # The stored answer's id, which feedback is posted against. None when nothing
    # was stored -- the daily-limit reply -- so there is nothing to rate.
    message_id: int | None
    classification: Classification | None
    citations: list[CitationOut]
    usage: Usage | None

    @classmethod
    def answered(cls, session_id: str, result: TurnResult) -> "ChatResponse":
        return cls(
            reply=result.reply,
            session_id=session_id,
            message_id=result.message_id,
            classification=result.classification,
            citations=_citations(result),
            usage=_usage(result),
        )

    @classmethod
    def refused(cls, session_id: str, reply: str) -> "ChatResponse":
        return cls(
            reply=reply,
            session_id=session_id,
            message_id=None,
            classification=None,
            citations=[],
            usage=None,
        )


# --- The payloads of the streamed events -------------------------------------
# One model per event, each becoming the `data:` line of a server-sent event. The
# event's name travels separately, on the `event:` line (see routers/chat.py).


class RouteData(BaseModel):
    """``event: route`` -- which of the three routes the message took."""

    classification: Classification


class TokenData(BaseModel):
    """``event: token`` -- the next piece of the answer, already through the link filter."""

    text: str


class DoneData(BaseModel):
    """``event: done`` -- the last event of an answer. Everything but the text, which
    has already arrived as tokens."""

    message_id: int | None
    classification: Classification | None
    citations: list[CitationOut]
    usage: Usage | None

    @classmethod
    def answered(cls, result: TurnResult) -> "DoneData":
        return cls(
            message_id=result.message_id,
            classification=result.classification,
            citations=_citations(result),
            usage=_usage(result),
        )

    @classmethod
    def refused(cls) -> "DoneData":
        return cls(message_id=None, classification=None, citations=[], usage=None)


class ErrorData(BaseModel):
    """``event: error`` -- the answer failed after the stream had started.

    ``status`` is the code the failure would have had as a plain response, so a
    client can treat the two the same way.
    """

    detail: str
    status: int


class FeedbackRequest(BaseModel):
    """A thumbs up or down on an answer, optionally with a sentence."""

    model_config = ConfigDict(extra="forbid")

    # Must be the conversation the answer belongs to; see db/feedback.py.
    session_id: SessionId
    # 1 for thumbs up, -1 for down. BeforeValidator runs on the raw value, before
    # the Literal check gets a chance to read `true` as 1.
    rating: Annotated[Literal[-1, 1], BeforeValidator(_not_a_boolean)]
    comment: Comment | None = None
