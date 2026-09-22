"""Rachel: one message in, an answer out, as it is written.

``respond()`` is the whole assistant, minus the database and minus HTTP. It
classifies the message, takes one of three routes, and yields events as things
happen -- so the API can stream an answer to a visitor, the terminal chat can print
it as it arrives, and the eval harness can ignore all that and keep the last event.

The three routes, and what each costs:

- ``out_of_scope`` -- a fixed reply. The classification call is the only spend.
- ``small_talk`` -- one streamed call with the conversation, no search.
- ``mihail_related`` -- the knowledge-base answer: the model is made to search
  first, then answers from what came back.

**The first search is required** (``tool_choice="required"``). The prompt already
says to search before answering any factual question, and this makes that true
rather than likely, the same way the link filter makes the link rule true rather
than likely. Two things follow: every answer about Mihail has a ``top_score``
recorded, which is the content-gap signal deliverable 4 is built on; and the search
call never has text to stream, so nothing reaches a visitor before the answer does.

Nothing here writes to the database. ``conversation.py`` adds memory and
persistence; keeping them apart is what lets the evals run thousands of questions
without filling the chat tables with things no visitor ever asked.
"""

import json
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import structlog
from openai.types.responses import EasyInputMessageParam, ResponseInputItemParam, ToolChoiceOptions

from portfolio_ai.assistant import memory, retrieval
from portfolio_ai.assistant.classifier import Classification, classify
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.assistant.postprocess import LinkFilter, is_fallback, is_forbidden_url
from portfolio_ai.assistant.prompts.loader import (
    ALL,
    OUT_OF_SCOPE_REPLY,
    RAG_AGENT,
    SMALL_TALK,
)
from portfolio_ai.config import ReasoningEffort, Settings, get_settings
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

log = structlog.get_logger(__name__)

# How many documents an answer says it drew on. Three, because the point of showing
# them is "here is where this came from", not a bibliography -- and with top_k at 20
# on an eleven-document corpus, a search touches most of the knowledge base.
MAX_CITATIONS = 3


@dataclass(frozen=True)
class AssistantConfig:
    """The knobs, gathered so a caller can change one without touching the process.

    Built from ``Settings`` by default. The evals in step 5 build it directly -- run
    the same dataset at ``chat_model="gpt-5"`` and compare -- and record it with the
    run, which is what keeps a score meaningful months later.
    """

    chat_model: str
    classifier_model: str
    classifier_effort: ReasoningEffort | None
    chat_effort: ReasoningEffort | None
    top_k: int
    max_search_rounds: int

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "AssistantConfig":
        settings = settings or get_settings()
        return cls(
            chat_model=settings.chat_model,
            classifier_model=settings.classifier_model,
            classifier_effort=settings.classifier_reasoning_effort,
            chat_effort=settings.chat_reasoning_effort,
            top_k=settings.retrieval_top_k,
            max_search_rounds=settings.agent_max_search_rounds,
        )

    def as_record(self) -> dict[str, object]:
        return {
            "chat_model": self.chat_model,
            "classifier_model": self.classifier_model,
            "classifier_effort": self.classifier_effort,
            "chat_effort": self.chat_effort,
            "top_k": self.top_k,
            "max_search_rounds": self.max_search_rounds,
            # Which text produced the answer, not just which model. A prompt edit
            # moves scores as much as a model change does.
            "prompts": [prompt.ref for prompt in ALL],
        }


@dataclass(frozen=True)
class Citation:
    """A document an answer drew on, for the chat UI to show under the reply."""

    doc_id: str
    title: str
    url: str | None
    section: str | None


@dataclass(frozen=True)
class Search:
    """One run of the search tool, as recorded on the stored message."""

    query: str
    chunk_ids: list[int]
    hits: int
    top_score: float | None
    duration_ms: int

    def as_record(self) -> dict[str, object]:
        return {
            "name": retrieval.TOOL_NAME,
            "query": self.query,
            "chunk_ids": self.chunk_ids,
            "hits": self.hits,
            "top_score": self.top_score,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class TurnResult:
    """Everything about one answer: the text, the evidence, the bill."""

    reply: str
    classification: Classification
    citations: list[Citation]
    searches: list[Search]
    retrieved_chunk_ids: list[int]
    # The best similarity score of any search in this turn. None when nothing was
    # searched, which is every small-talk and out-of-scope answer.
    top_score: float | None
    fallback_used: bool
    # How many forbidden links the filter took out. Should be zero: the prompt
    # forbids them. Worth watching, because a number above zero means the prompt
    # and the model disagree and only the code is stopping it.
    links_removed: int
    calls: list[CallUsage]
    model: str
    latency_ms: int
    # Time until the first word reached the visitor. The number that decides whether
    # the chat feels alive or broken, and the reason any of this streams.
    first_token_ms: int | None
    # Set by conversation.py once the turn is stored. None when nothing was stored.
    message_id: int | None = None

    @property
    def prompt_tokens(self) -> int:
        """Input tokens across the chat calls. Embeddings are a different model at a
        different price, so they are counted in ``cost`` but not here."""
        return sum(call.input_tokens for call in self.calls if call.step != "embed")

    @property
    def completion_tokens(self) -> int:
        return sum(call.output_tokens for call in self.calls if call.step != "embed")

    @property
    def cost(self) -> Decimal:
        return sum((call.cost for call in self.calls), Decimal(0))


@dataclass(frozen=True)
class Classified:
    """The route this message took."""

    label: Classification


@dataclass(frozen=True)
class Searched:
    """A search ran. Carries the chunks so a caller can show them; the API does not."""

    query: str
    chunks: list[RetrievedChunk]
    top_score: float | None


@dataclass(frozen=True)
class Token:
    """A piece of the answer, already through the link filter."""

    text: str


@dataclass(frozen=True)
class Done:
    """The last event of every turn."""

    result: TurnResult


type Event = Classified | Searched | Token | Done


@dataclass
class _Turn:
    """What accumulates while an answer is produced."""

    calls: list[CallUsage] = field(default_factory=list)
    searches: list[Search] = field(default_factory=list)
    # Chunks shown to the model, in the order they were shown, without repeats.
    chunks: list[RetrievedChunk] = field(default_factory=list)

    @property
    def seen(self) -> set[int]:
        return {chunk.chunk_id for chunk in self.chunks}

    @property
    def top_score(self) -> float | None:
        scores = [search.top_score for search in self.searches if search.top_score is not None]
        return max(scores) if scores else None


def _user(message: str) -> EasyInputMessageParam:
    return EasyInputMessageParam(role="user", content=message)


def _citations(chunks: Sequence[RetrievedChunk]) -> list[Citation]:
    """The documents behind the best-scoring chunks, each named once."""
    citations: list[Citation] = []

    for chunk in sorted(chunks, key=lambda chunk: chunk.score, reverse=True):
        if any(citation.doc_id == chunk.doc_id for citation in citations):
            continue
        citations.append(
            Citation(
                doc_id=chunk.doc_id,
                title=chunk.title,
                # The validator keeps forbidden URLs out of frontmatter, so this
                # should never fire. It costs one comparison to make sure the link
                # rule holds on this path too, and not only on the answer text.
                url=None if chunk.url and is_forbidden_url(chunk.url) else chunk.url,
                section=chunk.section,
            )
        )
        if len(citations) == MAX_CITATIONS:
            break

    return citations


def _query_from(call: responses.FunctionCall, message: str) -> str:
    """The search query the model asked for, or the visitor's own words if it did not.

    Strict schemas make malformed arguments close to impossible, and "close to" is
    the reason this exists: falling back to the raw message still answers the
    question, just with a worse query.
    """
    try:
        arguments = json.loads(call.arguments)
        query = str(arguments.get("query", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        query = ""

    if not query:
        log.warning("search_query_missing", arguments=call.arguments[:200])

    return query or message


async def _run_search(
    call: responses.FunctionCall, message: str, config: AssistantConfig, turn: _Turn
) -> tuple[ResponseInputItemParam, Searched]:
    """Run one tool call, returning what goes back to the model and what to report."""
    query = _query_from(call, message)
    result = await retrieval.search_knowledgebase(query, top_k=config.top_k, seen=turn.seen)

    turn.calls.append(result.embedding)
    turn.searches.append(
        Search(
            query=query,
            chunk_ids=[chunk.chunk_id for chunk in result.chunks],
            hits=result.hits,
            top_score=result.top_score,
            duration_ms=result.duration_ms,
        )
    )
    turn.chunks.extend(result.chunks)

    output: ResponseInputItemParam = {
        "type": "function_call_output",
        "call_id": call.call_id,
        "output": result.for_model(),
    }
    return output, Searched(query=query, chunks=result.chunks, top_score=result.top_score)


async def _knowledge_base_answer(
    message: str, history: Sequence[HistoryMessage], config: AssistantConfig, turn: _Turn
) -> AsyncIterator[str | Searched]:
    """The tool-calling loop: search, then answer. Yields raw text and search reports."""
    items: list[ResponseInputItemParam] = [*memory.as_input(history), _user(message)]
    rounds = 0

    while True:
        # First call: search, no choice about it. Last allowed call: answer, no
        # choice about that either. In between the model decides -- one search is
        # almost always enough on a corpus this size, and a second is for questions
        # that turn out to have two halves.
        first = rounds == 0
        out_of_rounds = rounds >= config.max_search_rounds
        tool_choice: ToolChoiceOptions = (
            "required" if first else "none" if out_of_rounds else "auto"
        )

        completed: responses.Completed | None = None
        async for event in responses.stream(
            step="search" if first else "answer",
            model=config.chat_model,
            instructions=RAG_AGENT.text,
            input=items,
            tools=[retrieval.TOOL],
            tool_choice=tool_choice,
            effort=config.chat_effort,
            prompt=RAG_AGENT.ref,
        ):
            if isinstance(event, responses.Completed):
                completed = event
            elif first:
                # A tool call is guaranteed on this round, so any text is the model
                # thinking out loud before searching ("Let me look that up"). n8n
                # discarded it too: its agent only ever returned the final answer.
                log.debug("preamble_discarded", length=len(event.text))
            else:
                yield event.text

        if completed is None:  # pragma: no cover - stream() raises before this
            raise AssertionError("the stream ended without a completed response")

        turn.calls.append(completed.usage)

        if not completed.function_calls:
            return

        # Everything the model produced goes back with the results, reasoning items
        # included: that is what lets the answering call pick up where the search
        # left off instead of starting again.
        items += completed.output

        for call in completed.function_calls:
            if call.name != retrieval.TOOL_NAME:  # pragma: no cover - only one tool exists
                log.warning("unknown_tool_called", name=call.name)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": f"There is no tool called {call.name}.",
                    }
                )
                continue

            # One at a time rather than concurrently: the second search skips what
            # the first returned, and that only means anything in a fixed order.
            output, searched = await _run_search(call, message, config, turn)
            items.append(output)
            yield searched

        rounds += 1


async def _small_talk(
    message: str, history: Sequence[HistoryMessage], config: AssistantConfig, turn: _Turn
) -> AsyncIterator[str]:
    """One short reply, with the conversation for context and no tools."""
    async for event in responses.stream(
        step="small_talk",
        model=config.chat_model,
        instructions=SMALL_TALK.text,
        input=[*memory.as_input(history), _user(message)],
        effort=config.chat_effort,
        prompt=SMALL_TALK.ref,
    ):
        if isinstance(event, responses.TextDelta):
            yield event.text
        else:
            turn.calls.append(event.usage)


async def _route(
    label: Classification,
    message: str,
    history: Sequence[HistoryMessage],
    config: AssistantConfig,
    turn: _Turn,
) -> AsyncIterator[str | Searched]:
    if label == "out_of_scope":
        # The fixed reply, and no second call. Roughly half the cost of a turn is
        # avoided here, which is the whole reason the classifier exists.
        yield OUT_OF_SCOPE_REPLY.text
    elif label == "small_talk":
        async for text in _small_talk(message, history, config, turn):
            yield text
    else:
        async for item in _knowledge_base_answer(message, history, config, turn):
            yield item


async def respond(
    message: str,
    history: Sequence[HistoryMessage] = (),
    *,
    config: AssistantConfig | None = None,
) -> AsyncIterator[Event]:
    """Answer one message. Yields events; the last is always :class:`Done`.

    ``history`` is the conversation so far, oldest first, and is not written to --
    the caller owns it. Nothing here touches the chat tables.
    """
    config = config or AssistantConfig.from_settings()
    started = time.perf_counter()
    turn = _Turn()
    links = LinkFilter()
    reply: list[str] = []
    first_token: float | None = None

    classified = await classify(
        message,
        memory.previous_exchange(history),
        model=config.classifier_model,
        effort=config.classifier_effort,
    )
    turn.calls.append(classified.usage)
    yield Classified(classified.label)

    async for item in _route(classified.label, message, history, config, turn):
        if isinstance(item, Searched):
            yield item
            continue

        visible = links.feed(item)
        if visible:
            first_token = first_token or time.perf_counter()
            reply.append(visible)
            yield Token(visible)

    tail = links.finish()
    if tail:
        first_token = first_token or time.perf_counter()
        reply.append(tail)
        yield Token(tail)

    answer = "".join(reply)
    fallback = classified.label == "mihail_related" and is_fallback(answer)

    result = TurnResult(
        reply=answer,
        classification=classified.label,
        # No citations on an answer that says it has none: "I don't have that
        # information" with three sources under it reads as a contradiction.
        citations=[] if fallback else _citations(turn.chunks),
        searches=turn.searches,
        retrieved_chunk_ids=[chunk.chunk_id for chunk in turn.chunks],
        top_score=turn.top_score,
        fallback_used=fallback,
        links_removed=links.removed,
        calls=turn.calls,
        # The model that wrote the answer, as the API named it. For an out-of-scope
        # reply that is the classifier's, because it is the only call there was.
        model=turn.calls[-1].model,
        latency_ms=int((time.perf_counter() - started) * 1000),
        first_token_ms=int((first_token - started) * 1000) if first_token else None,
    )

    if result.links_removed:
        log.warning("forbidden_links_removed", count=result.links_removed)

    # Counts and costs; never the question or the answer. The database keeps those,
    # under a retention policy that logs do not have.
    log.info(
        "turn_answered",
        classification=result.classification,
        searches=len(result.searches),
        top_score=result.top_score,
        fallback_used=result.fallback_used,
        calls=len(result.calls),
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=str(result.cost),
        latency_ms=result.latency_ms,
        first_token_ms=result.first_token_ms,
    )

    yield Done(result)
