"""The three routes, the search loop, and what ends up on the result.

No database and no network: the OpenAI API is the fake, and the one SQL query is
replaced with a fixed set of chunks. Everything between -- classification, the
forced first search, sending results back, streaming, the link filter, the
bookkeeping -- is the real code.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.assistant import agent
from portfolio_ai.assistant.agent import AssistantConfig, Done, Event, Searched, Token, TurnResult
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.assistant.prompts.loader import OUT_OF_SCOPE_REPLY
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.llm import responses

CONFIG = AssistantConfig(
    chat_model="gpt-5-mini",
    classifier_model="gpt-5-mini",
    classifier_effort=None,
    chat_effort=None,
    top_k=3,
    max_search_rounds=3,
)

CHUNKS = [
    RetrievedChunk(
        chunk_id=1,
        doc_id="tech-stack",
        title="Tech Stack",
        url="https://mihaylov.io/#about",
        section="php-laravel",
        section_title="Does Mihail work with Laravel?",
        content="Does Mihail work with Laravel?\n\nYes, it is his main PHP framework.",
        score=0.62,
    ),
    RetrievedChunk(
        chunk_id=2,
        doc_id="tech-stack",
        title="Tech Stack",
        url="https://mihaylov.io/#about",
        section="javascript",
        section_title="What JavaScript does he use?",
        content="What JavaScript does he use?\n\nVue and Nuxt, mostly.",
        score=0.48,
    ),
    RetrievedChunk(
        chunk_id=3,
        doc_id="hiring",
        title="Hiring Mihail",
        url="https://mihaylov.io/#contact",
        section="freelance",
        section_title="Is he available?",
        content="Is he available?\n\nFor selected freelance work.",
        score=0.31,
    ),
]


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


@pytest.fixture(autouse=True)
def corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the one SQL query with a fixed knowledge base."""

    # Both arguments are the real signature's, and the vector is ignored on purpose:
    # the fake embeddings all point the same way, so the ranking here is fixed.
    async def search_chunks(  # ruff: ignore[unused-async]
        vector: list[float],  # ruff: ignore[unused-function-argument]
        *,
        top_k: int,
    ) -> list[RetrievedChunk]:
        return CHUNKS[:top_k]

    monkeypatch.setattr(docs_db, "search_chunks", search_chunks)


async def _run(
    message: str, history: tuple[HistoryMessage, ...] = (), config: AssistantConfig = CONFIG
) -> tuple[list[Event], TurnResult]:
    events = [event async for event in agent.respond(message, history, config=config)]

    assert isinstance(events[-1], Done), "the last event is always Done"
    assert not [event for event in events[:-1] if isinstance(event, Done)], "exactly one Done"

    return events, events[-1].result


def _text(events: list[Event]) -> str:
    return "".join(event.text for event in events if isinstance(event, Token))


# --- routing ----------------------------------------------------------------


async def test_out_of_scope_is_answered_without_a_second_call(fake: FakeOpenAI) -> None:
    fake.classify("out_of_scope")

    events, result = await _run("What is the weather in Sofia?")

    assert result.reply == OUT_OF_SCOPE_REPLY.text
    assert _text(events) == OUT_OF_SCOPE_REPLY.text
    assert len(fake.requests) == 1, "the classification is the only call"
    assert result.searches == []
    assert result.top_score is None


async def test_small_talk_answers_without_searching(fake: FakeOpenAI) -> None:
    fake.classify("small_talk")
    fake.say("Hi, how can I help?")

    _, result = await _run("Hi")

    assert result.reply == "Hi, how can I help?"
    assert result.classification == "small_talk"
    assert result.searches == []
    assert "tools" not in fake.requests[1]
    assert not fake.embedded, "small talk never embeds anything"


async def test_a_question_about_mihail_searches_before_answering(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is the PHP framework Mihail uses most.")

    events, result = await _run("Does Mihail work with Laravel?")

    assert result.reply == "Yes, Laravel is the PHP framework Mihail uses most."
    assert fake.requests[1]["tool_choice"] == "required", "the first call must search"
    assert fake.embedded == ["Laravel PHP experience"], "the model's query, not the question"
    assert [search.query for search in result.searches] == ["Laravel PHP experience"]
    assert result.top_score == pytest.approx(0.62)
    assert result.retrieved_chunk_ids == [1, 2, 3]
    assert [event.query for event in events if isinstance(event, Searched)] == [
        "Laravel PHP experience"
    ]


async def test_the_search_results_go_back_to_the_model(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes.")

    await _run("Does Mihail work with Laravel?")

    sent_back = fake.requests[2]["input"][-1]
    assert sent_back["type"] == "function_call_output"
    assert sent_back["call_id"].startswith("call_")
    assert "main PHP framework" in sent_back["output"]
    assert "0.62" not in sent_back["output"], "n8n showed no scores, and neither do we"


async def test_text_written_alongside_the_forced_search_is_dropped(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience", preamble="Let me look that up for you.")
    fake.say("Yes, he does.")

    _, result = await _run("Does Mihail work with Laravel?")

    assert result.reply == "Yes, he does."


# --- what the model is shown ------------------------------------------------


async def test_the_classifier_is_given_the_previous_exchange(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Threadline project details")
    fake.say("Threadline is his most recent project.")

    history = (
        HistoryMessage("user", "What projects has Mihail worked on?"),
        HistoryMessage("assistant", "Threadline, among others. Want more detail?"),
    )
    await _run("Yes please", history)

    assert fake.requests[0]["input"] == [
        {"role": "user", "content": "What projects has Mihail worked on?"},
        {"role": "assistant", "content": "Threadline, among others. Want more detail?"},
        {"role": "user", "content": "Yes please"},
    ]


async def test_the_conversation_is_given_to_the_answering_model(fake: FakeOpenAI) -> None:
    fake.classify("small_talk")
    fake.say("You're welcome.")

    history = (
        HistoryMessage("user", "Does he use Laravel?"),
        HistoryMessage("assistant", "Yes, it is his main framework."),
    )
    await _run("Thanks!", history)

    assert fake.requests[1]["input"] == [
        {"role": "user", "content": "Does he use Laravel?"},
        {"role": "assistant", "content": "Yes, it is his main framework."},
        {"role": "user", "content": "Thanks!"},
    ]


# --- the search loop --------------------------------------------------------


async def test_a_second_search_does_not_repeat_the_first_ones_chunks(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.search("freelance availability")
    fake.say("Yes to both.")

    _, result = await _run("Does he use Laravel, and is he free?")

    assert [search.chunk_ids for search in result.searches] == [[1, 2, 3], []]
    assert result.retrieved_chunk_ids == [1, 2, 3], "each chunk is shown once"
    assert "No new results" in fake.requests[3]["input"][-1]["output"]


async def test_searching_stops_after_the_configured_number_of_rounds(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("first")
    fake.search("second")
    fake.say("Answering now.")

    config = AssistantConfig(**{**vars(CONFIG), "max_search_rounds": 2})
    _, result = await _run("A hard question", config=config)

    assert [request.get("tool_choice") for request in fake.requests[1:]] == [
        "required",
        "auto",
        "none",
    ]
    assert result.reply == "Answering now."


def test_a_search_request_that_does_not_parse_searches_for_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The visitor's own words still find something, just less precisely. The log
    line says it happened and how long the arguments were, never what they said:
    they are the model's rewrite of the visitor's question."""
    logged: list[dict[str, object]] = []

    def warning(event: str, **fields: object) -> None:
        logged.append({"event": event, **fields})

    monkeypatch.setattr(agent, "log", SimpleNamespace(warning=warning))
    call = responses.FunctionCall(
        call_id="call_1", name="search_knowledgebase", arguments='{"query": "hiring at Acme'
    )

    query = agent._query_from(call, "Is Mihail open to a Laravel role?")

    assert query == "Is Mihail open to a Laravel role?"
    assert logged == [{"event": "search_query_missing", "arguments_length": len(call.arguments)}]


# --- the result -------------------------------------------------------------


async def test_a_forbidden_link_never_reaches_the_visitor(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Threadline")
    fake.say("Read more at https://mihaylov.io/projects/threadline today.")

    events, result = await _run("Tell me about Threadline")

    assert result.reply == "Read more at  today."
    assert _text(events) == result.reply, "what was streamed is what was stored"
    assert result.links_removed == 1


async def test_a_fallback_answer_is_flagged_and_cites_nothing(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("favourite football team")
    fake.say("I don't have that information in my knowledge base.")

    _, result = await _run("What is his favourite football team?")

    assert result.fallback_used
    assert result.citations == []
    assert result.top_score == pytest.approx(0.62), "the score is still recorded"


async def test_citations_are_the_best_documents_named_once_each(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel.")

    _, result = await _run("Does Mihail work with Laravel?")

    assert [citation.doc_id for citation in result.citations] == ["tech-stack", "hiring"]
    assert result.citations[0].url == "https://mihaylov.io/#about"
    assert result.citations[0].section == "php-laravel"


async def test_every_call_is_costed_and_the_timings_recorded(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel.")

    _, result = await _run("Does Mihail work with Laravel?")

    assert [call.step for call in result.calls] == ["classify", "search", "embed", "answer"]
    assert result.prompt_tokens == 300, "three chat calls at 100 input tokens each"
    assert result.completion_tokens == 60
    assert result.cost > Decimal(0)
    assert result.first_token_ms is not None
    assert result.model == "gpt-5-mini-2025-08-07"
    assert result.message_id is None, "nothing was stored"


async def test_an_unreadable_classification_falls_back_to_answering(fake: FakeOpenAI) -> None:
    fake.unparseable()
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel.")

    _, result = await _run("Does Mihail work with Laravel?")

    assert result.classification == "mihail_related"
