"""The Responses API adapter, against the real SDK and a fake network."""

from decimal import Decimal

import pytest
from openai_fake import FakeOpenAI, install_fake_openai
from pydantic import BaseModel

from portfolio_ai.exceptions import AssistantError, ConfigError
from portfolio_ai.llm import responses


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


async def _collect(**kwargs: object) -> tuple[str, responses.Completed]:
    """Run one streamed call; return the text it streamed and its Completed."""
    kwargs = {
        "step": "test",
        "model": "gpt-5-mini",
        "instructions": "Be brief.",
        "input": [],
    } | kwargs
    text, completed = "", None
    async for event in responses.stream(**kwargs):  # type: ignore[arg-type]
        if isinstance(event, responses.TextDelta):
            text += event.text
        else:
            completed = event
    assert completed is not None
    return text, completed


class Answer(BaseModel):
    category: str


async def test_text_streams_in_pieces_and_ends_with_one_completed(fake: FakeOpenAI) -> None:
    fake.say("Laravel is the PHP framework Mihail uses most.")

    text, completed = await _collect()

    assert text == "Laravel is the PHP framework Mihail uses most."
    assert completed.function_calls == []


async def test_usage_is_recorded_and_priced_from_the_snapshot_name(fake: FakeOpenAI) -> None:
    fake.say("Hi.")

    _, completed = await _collect(prompt="small_talk@1")

    usage = completed.usage
    assert (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens) == (100, 20, 8)
    assert usage.model == "gpt-5-mini-2025-08-07"
    assert usage.prompt == "small_talk@1"
    assert usage.cost > Decimal(0)


async def test_tool_calls_come_back_with_their_call_ids(fake: FakeOpenAI) -> None:
    fake.search("Laravel PHP experience", "freelance availability")

    text, completed = await _collect(tools=[{"type": "function", "name": "search_knowledgebase"}])

    assert not text
    assert [call.arguments for call in completed.function_calls] == [
        '{"query": "Laravel PHP experience"}',
        '{"query": "freelance availability"}',
    ]
    assert len({call.call_id for call in completed.function_calls}) == 2


async def test_every_call_is_stateless(fake: FakeOpenAI) -> None:
    """store=False, with reasoning asked for in encrypted form so it can travel back."""
    fake.say("Hi.")

    await _collect()

    sent = fake.requests[0]
    assert sent["store"] is False
    assert sent["include"] == ["reasoning.encrypted_content"]
    assert sent["stream"] is True


async def test_output_goes_back_as_input_with_the_reasoning_still_sealed(fake: FakeOpenAI) -> None:
    fake.search("Laravel")
    fake.say("Yes.")

    _, first = await _collect(tools=[{"type": "function", "name": "search_knowledgebase"}])
    call = first.function_calls[0]
    await _collect(
        input=[
            *first.output,
            {"type": "function_call_output", "call_id": call.call_id, "output": "[]"},
        ]
    )

    sent_back = fake.requests[1]["input"]
    assert sent_back[0] == {
        "type": "reasoning",
        "id": "rs_fake",
        "summary": [],
        "encrypted_content": "sealed",
    }
    assert sent_back[1]["call_id"] == call.call_id
    assert sent_back[2] == {"type": "function_call_output", "call_id": call.call_id, "output": "[]"}


async def test_no_effort_means_nothing_is_sent(fake: FakeOpenAI) -> None:
    fake.say("Hi.")
    fake.say("Hi.")

    await _collect()
    await _collect(effort="minimal")

    assert "reasoning" not in fake.requests[0]
    assert fake.requests[1]["reasoning"] == {"effort": "minimal"}


async def test_tool_choice_is_only_sent_with_tools(fake: FakeOpenAI) -> None:
    fake.say("Hi.")
    fake.search("x")

    await _collect(tool_choice="required")
    await _collect(
        tools=[{"type": "function", "name": "search_knowledgebase"}], tool_choice="required"
    )

    assert "tool_choice" not in fake.requests[0]
    assert fake.requests[1]["tool_choice"] == "required"


async def test_a_failed_response_becomes_an_assistant_error(fake: FakeOpenAI) -> None:
    fake.fail()

    with pytest.raises(AssistantError, match="failed"):
        await _collect()


async def test_a_server_error_becomes_an_assistant_error(fake: FakeOpenAI) -> None:
    fake.http_error(500)

    with pytest.raises(AssistantError, match="InternalServerError"):
        await _collect()


async def test_a_rejected_key_is_a_configuration_error(fake: FakeOpenAI) -> None:
    fake.http_error(401)

    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        await _collect()


async def test_parse_returns_the_structured_value(fake: FakeOpenAI) -> None:
    fake.classify("small_talk")

    result = await responses.parse(
        step="classify", model="gpt-5-mini", instructions="Classify.", input=[], text_format=Answer
    )

    assert result.value == Answer(category="small_talk")
    assert result.usage.input_tokens == 100
    assert fake.requests[0]["store"] is False


async def test_parse_returns_none_when_the_output_does_not_fit_the_schema(
    fake: FakeOpenAI,
) -> None:
    class Strict(BaseModel):
        count: int

    fake.classify("small_talk")  # {"category": ...}, which Strict cannot hold

    result = await responses.parse(
        step="classify", model="gpt-5-mini", instructions="Classify.", input=[], text_format=Strict
    )

    assert result.value is None
    assert result.usage.cost == Decimal(0)
