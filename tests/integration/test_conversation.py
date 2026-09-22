"""Two turns of a conversation, end to end.

Real database, real migrations, real retrieval, real SQL. Only the model is a fake.
This is the test that would fail if memory were not loaded, if a turn were stored
without its analytics, or if the follow-up question reached the classifier without
the exchange that gives it meaning.
"""

from typing import Any

import corpus
import pytest
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.assistant.agent import Done, TurnResult
from portfolio_ai.assistant.conversation import chat
from portfolio_ai.db.pool import get_pool

pytestmark = pytest.mark.integration

SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


@pytest.fixture(autouse=True)
async def knowledge_base() -> None:
    await corpus.clear()
    # The fake embeds every query to the same vector, axis 0, so the first section
    # here is always the best match and the ranking is known.
    await corpus.seed(
        "tech-stack",
        "Tech Stack",
        [
            (
                "Does Mihail work with Laravel?",
                "Yes, it is his main PHP framework.",
                corpus.axis(0),
            ),
            ("What about Vue?", "Vue and Nuxt on the front end.", corpus.between(0, 1, 0.6)),
        ],
        url="https://mihaylov.io/#about",
    )


async def _turn(message: str) -> TurnResult:
    result: TurnResult | None = None
    async for event in chat(SESSION, message):
        if isinstance(event, Done):
            result = event.result

    assert result is not None
    return result


async def _rows() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select * from chat_messages order by id")
        return [dict(row) for row in await cur.fetchall()]


async def test_a_conversation_is_answered_remembered_and_recorded(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework. Want more detail?")

    first = await _turn("Does Mihail work with Laravel?")

    fake.classify("mihail_related")
    fake.search("Laravel projects detail")
    fake.say("He has used it on most of his recent work.")

    second = await _turn("Yes please")

    # --- what the visitor got
    assert first.reply.startswith("Yes, Laravel")
    assert second.reply == "He has used it on most of his recent work."
    assert first.message_id is not None
    assert second.message_id is not None

    # --- what the classifier was shown the second time
    assert fake.requests[3]["input"] == [
        {"role": "user", "content": "Does Mihail work with Laravel?"},
        {"role": "assistant", "content": first.reply},
        {"role": "user", "content": "Yes please"},
    ]

    # --- and what the answering model was shown
    assert fake.requests[4]["input"][:2] == [
        {"role": "user", "content": "Does Mihail work with Laravel?"},
        {"role": "assistant", "content": first.reply},
    ]

    # --- what is in the database
    rows = await _rows()
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1]["reply_to_id"] == rows[0]["id"]
    assert rows[3]["reply_to_id"] == rows[2]["id"]
    assert rows[3]["id"] == second.message_id

    answer = rows[1]
    assert answer["classification"] == "mihail_related"
    assert answer["top_score"] == pytest.approx(1.0), "the fake query matches section one exactly"
    assert answer["fallback_used"] is False
    assert len(answer["retrieved_chunk_ids"]) == 2
    assert [call["step"] for call in answer["llm_calls"]] == [
        "classify",
        "search",
        "embed",
        "answer",
    ]
    assert answer["first_token_ms"] is not None
    assert answer["cost_usd"] > 0


async def test_the_second_turn_reuses_the_session(fake: FakeOpenAI) -> None:
    for _ in range(2):
        fake.classify("small_talk")
        fake.say("Hi there.")

    await _turn("Hi")
    await _turn("Hello again")

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select session_id from chat_sessions")
        sessions = await cur.fetchall()

    assert [row["session_id"] for row in sessions] == [SESSION]


async def test_a_fallback_answer_is_recorded_with_the_score_that_produced_it(
    fake: FakeOpenAI,
) -> None:
    fake.classify("mihail_related")
    fake.search("favourite football team")
    fake.say("I don't have that information in my knowledge base.")

    result = await _turn("What is his favourite football team?")

    rows = await _rows()
    assert rows[1]["fallback_used"] is True
    assert rows[1]["top_score"] is not None, "a gap is only useful next to its score"
    assert result.citations == []
