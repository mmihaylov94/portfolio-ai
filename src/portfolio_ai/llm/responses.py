"""Calls to OpenAI's Responses API: one that returns a typed object, one that streams.

The Responses API rather than Chat Completions, which n8n used. It is the API
OpenAI builds for reasoning models like gpt-5-mini, and one feature decides it: when
the model calls a tool and is then called again with the result, its reasoning from
the first call can be handed back to the second. With Chat Completions that
reasoning is thrown away between calls, and the answer is written by a model that
has forgotten why it searched for what it did.

Everything is sent with ``store=False``. By default the API keeps each response on
OpenAI's side, retrievable by id, which this project has no use for -- and these
are visitors' conversations. The cost of saying no is that nothing can be looked up
later by id, so the reasoning travels back to the next call inside the request
itself, encrypted (``include=["reasoning.encrypted_content"]``).

This module is deliberately thin. It does not retry, back off or time out -- the SDK
does all three, configured in ``client.py``. What it adds is the three things every
caller would otherwise repeat:

- **usage, recorded per call**: tokens (cached and reasoning separately), latency
  and cost, in one :class:`CallUsage`
- **the stream reduced to two events**: text as it arrives, and a final
  :class:`Completed` carrying the output items and any tool calls
- **one domain error**: every failure becomes :class:`AssistantError`, apart from a
  rejected key, which is configuration and stays a :class:`ConfigError`
"""

import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import pydantic
import structlog
from openai import APIError, AuthenticationError, PermissionDeniedError, not_given, omit
from openai.types.responses import (
    FunctionToolParam,
    Response,
    ResponseInputItemParam,
    ResponseStreamEvent,
    ResponseUsage,
    ToolChoiceOptions,
)
from openai.types.shared_params import Reasoning

from portfolio_ai.config import ReasoningEffort
from portfolio_ai.exceptions import AssistantError, ConfigError, PortfolioAIError
from portfolio_ai.llm.client import get_client
from portfolio_ai.llm.pricing import cost_usd

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CallUsage:
    """What one OpenAI call consumed. One of these is recorded for every call.

    ``input_tokens`` includes ``cached_tokens`` and ``output_tokens`` includes
    ``reasoning_tokens``, which is how the API reports them. The breakdown is kept
    because both change the picture: cached input costs a tenth of the price, and
    reasoning tokens are paid for but never seen.
    """

    step: str
    model: str
    # Which prompt produced this call, as "rag_agent@1". None for calls that have no
    # prompt, like embeddings.
    prompt: str | None
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    reasoning_tokens: int
    latency_ms: int
    cost: Decimal

    def as_record(self) -> dict[str, object]:
        """The shape stored in ``chat_messages.llm_calls``.

        Cost as a string, because JSON has no decimal type -- writing it as a
        float would reintroduce exactly the rounding ``Decimal`` exists to avoid.
        """
        return {
            "step": self.step,
            "model": self.model,
            "prompt": self.prompt,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": str(self.cost),
        }


@dataclass(frozen=True)
class Parsed[T]:
    """A structured result, or None if the model's output could not be used."""

    value: T | None
    usage: CallUsage


@dataclass(frozen=True)
class FunctionCall:
    """The model asking for a tool to be run."""

    # Echoed back with the result, so the API can pair each output with its call.
    call_id: str
    name: str
    # JSON, as a string. Strict schemas guarantee it parses; the caller still
    # decides what to do with it.
    arguments: str


@dataclass(frozen=True)
class TextDelta:
    """A piece of the answer, as it is generated."""

    text: str


@dataclass(frozen=True)
class Completed:
    """The end of one streamed call.

    ``output`` is every item the model produced -- reasoning, messages, tool calls
    -- already in the shape the next request's ``input`` takes. A tool-calling loop
    appends it, appends the tool results, and calls again.
    """

    output: list[ResponseInputItemParam]
    function_calls: list[FunctionCall]
    usage: CallUsage


def _reasoning(effort: ReasoningEffort | None) -> Reasoning | None:
    # None means "send nothing", so the model's own default applies. That default
    # is what n8n sent, since it never set an effort either.
    return {"effort": effort} if effort else None


def _usage(response: Response, *, step: str, prompt: str | None, started: float) -> CallUsage:
    usage: ResponseUsage | None = response.usage
    input_tokens = usage.input_tokens if usage else 0
    cached_tokens = usage.input_tokens_details.cached_tokens if usage else 0
    output_tokens = usage.output_tokens if usage else 0
    reasoning_tokens = usage.output_tokens_details.reasoning_tokens if usage else 0

    record = CallUsage(
        step=step,
        model=response.model,
        prompt=prompt,
        input_tokens=input_tokens,
        cached_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        latency_ms=int((time.perf_counter() - started) * 1000),
        cost=cost_usd(response.model, input_tokens, output_tokens, cached_tokens),
    )

    # Numbers only. Nothing a visitor typed goes into a log line: the database has
    # a retention policy and logs do not.
    log.info("llm_call", **record.as_record())
    return record


def _empty_usage(step: str, model: str, prompt: str | None, started: float) -> CallUsage:
    """Usage for a call whose response could not be read, so has no figures."""
    return CallUsage(
        step=step,
        model=model,
        prompt=prompt,
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        latency_ms=int((time.perf_counter() - started) * 1000),
        cost=Decimal(0),
    )


def domain_error(exc: APIError) -> PortfolioAIError:
    """Map an SDK failure onto this project's errors.

    ``APIError`` is the only thing that needs catching. The SDK raises it for every
    failure it knows about, and wraps the network ones itself -- a dropped
    connection, even one halfway through reading a stream, arrives as
    ``APIConnectionError``, and a timeout as ``APITimeoutError``, both subclasses.
    Worth knowing because the SDK does not use the ``httpx`` this project uses for
    GitHub: since version 3 it runs on a separate package, ``httpx2``, so catching
    ``httpx`` errors around an SDK call would look careful and catch nothing.

    Public because retrieval needs it too: the query embedding is an OpenAI call
    made in the middle of answering, and it should fail the same way these do.
    """
    if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
        return ConfigError(
            f"OpenAI rejected the API key. Check OPENAI_API_KEY in .env ({type(exc).__name__})."
        )
    return AssistantError(f"OpenAI call failed ({type(exc).__name__}): {exc.message}")


# Seven keyword-only arguments, which pylint's rule counts as too many. The rule is
# about call sites like f(a, b, c, d, e, g) where position carries the meaning; every
# argument here has to be named at the call, which is the problem already solved.
async def parse[T: pydantic.BaseModel](  # ruff: ignore[too-many-arguments]
    *,
    step: str,
    model: str,
    instructions: str,
    input: Sequence[ResponseInputItemParam],  # ruff: ignore[builtin-argument-shadowing]
    text_format: type[T],
    effort: ReasoningEffort | None = None,
    prompt: str | None = None,
    request_timeout: float | None = None,
) -> Parsed[T]:
    """One call whose answer is an instance of ``text_format``.

    ``request_timeout`` overrides ``OPENAI_TIMEOUT_SECONDS`` for this call alone, for a
    caller that knows its call is slower than a visitor-facing one should be.

    The model is constrained to ``text_format``'s JSON schema by the API itself
    (structured outputs), so it cannot reply with prose. It can still refuse, or be
    cut off -- both come back as ``value=None`` for the caller to decide about,
    rather than an exception, because a sensible fallback usually exists.

    ``input`` shadows the builtin of the same name. It is the SDK's parameter name,
    and matching it means a reader can move between this and OpenAI's documentation
    without translating.
    """
    started = time.perf_counter()

    try:
        response = await get_client().responses.parse(
            model=model,
            instructions=instructions,
            input=list(input),
            text_format=text_format,
            reasoning=_reasoning(effort) or omit,
            store=False,
            # not_given, unlike None, keeps the client's own timeout: None would
            # mean "no timeout at all".
            timeout=request_timeout if request_timeout is not None else not_given,
        )
    except pydantic.ValidationError:
        # The output was JSON that did not match the schema -- a truncated reply,
        # typically. No usage figures come back with this, so nothing to record.
        log.warning("llm_output_unusable", step=step, model=model)
        return Parsed(value=None, usage=_empty_usage(step, model, prompt, started))
    except APIError as exc:
        raise domain_error(exc) from exc

    return Parsed(
        value=response.output_parsed,
        usage=_usage(response, step=step, prompt=prompt, started=started),
    )


def _read(event: ResponseStreamEvent) -> TextDelta | Response | None:
    """What one stream event means here: some text, the finished response, or nothing.

    The API sends a dozen or so event types -- an output item was added, a content
    part began, a tool call's arguments grew by a few characters. A caller that
    wants text as it arrives and the whole response at the end needs four of them.
    """
    if event.type == "response.output_text.delta":
        return TextDelta(event.delta)

    if event.type == "response.completed":
        return event.response

    if event.type == "response.failed":
        error = event.response.error
        detail = f"{error.code}: {error.message}" if error else "no reason given"
        raise AssistantError(f"OpenAI reported the response failed ({detail})")

    if event.type == "response.incomplete":
        details = event.response.incomplete_details
        reason = details.reason if details else None
        raise AssistantError(f"OpenAI stopped the response early ({reason or 'no reason given'})")

    return None


async def stream(  # ruff: ignore[too-many-arguments] -- keyword-only; see parse()
    *,
    step: str,
    model: str,
    instructions: str,
    input: Sequence[ResponseInputItemParam],  # ruff: ignore[builtin-argument-shadowing]
    tools: Sequence[FunctionToolParam] = (),
    tool_choice: ToolChoiceOptions = "auto",
    effort: ReasoningEffort | None = None,
    prompt: str | None = None,
) -> AsyncIterator[TextDelta | Completed]:
    """One streamed call: yields text as it arrives, then exactly one ``Completed``.

    An async generator -- a function that ``yield``s inside ``async def``. The
    caller consumes it with ``async for``, and each item arrives the moment the API
    sends it, which is what lets the first words of an answer reach a visitor while
    the rest is still being written.
    """
    started = time.perf_counter()

    try:
        events = await get_client().responses.create(
            model=model,
            instructions=instructions,
            input=list(input),
            tools=list(tools) or omit,
            tool_choice=tool_choice if tools else omit,
            reasoning=_reasoning(effort) or omit,
            store=False,
            include=["reasoning.encrypted_content"],
            stream=True,
        )
    except APIError as exc:
        raise domain_error(exc) from exc

    final: Response | None = None

    # The HTTP stream is open for as long as this block is, and it has to close even
    # when the caller stops listening -- a visitor closes the tab mid-answer. Left
    # open, OpenAI keeps writing (and billing) an answer nobody will read.
    #
    # ruff flags the yield below, and it is pointing at something real: a generator
    # abandoned half-way through is not closed at that moment, but later, when the
    # event loop finalises it. In CPython that is as soon as the last reference to it
    # goes, which is promptly -- and callers that stop early on purpose close it
    # themselves. The alternative the rule suggests, an async context manager, would
    # have to spread to every caller up to the HTTP endpoint.
    async with events:
        try:
            async for event in events:
                item = _read(event)
                if isinstance(item, TextDelta):
                    yield item  # ruff: ignore[yield-in-context-manager-in-async-generator]
                elif item is not None:
                    final = item
        except APIError as exc:
            raise domain_error(exc) from exc

    if final is None:
        raise AssistantError("The stream ended without a completed response")

    yield Completed(
        # Exactly how the SDK serialises a model it is handed as input, so what goes
        # back is what the SDK itself would have sent. Typed as the request shape,
        # which is what these dictionaries are.
        output=[
            cast("ResponseInputItemParam", item.model_dump(exclude_unset=True, mode="json"))
            for item in final.output
        ],
        function_calls=[
            FunctionCall(call_id=item.call_id, name=item.name, arguments=item.arguments)
            for item in final.output
            if item.type == "function_call"
        ],
        usage=_usage(final, step=step, prompt=prompt, started=started),
    )
