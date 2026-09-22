"""Storing sessions and turns, against a real Postgres.

The columns these tests care about are the ones a unit test cannot check: a jsonb
column rejecting a plain string, an array of chunk ids surviving the round trip, a
foreign key from an answer to its question, and a cascade that takes a whole
conversation with its session.
"""

import datetime as dt
from decimal import Decimal

import corpus
import pytest

from portfolio_ai.assistant.agent import Search, TurnResult
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.llm.responses import CallUsage

pytestmark = pytest.mark.integration

SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()


def _result(**overrides: object) -> TurnResult:
    defaults: dict[str, object] = {
        "reply": "Yes, Laravel is his main PHP framework.",
        "classification": "mihail_related",
        "citations": [],
        "searches": [
            Search(
                query="Laravel PHP experience",
                chunk_ids=[7, 8],
                hits=20,
                top_score=0.62,
                duration_ms=310,
            )
        ],
        "retrieved_chunk_ids": [7, 8],
        "top_score": 0.62,
        "fallback_used": False,
        "links_removed": 0,
        "calls": [
            CallUsage(
                step="classify",
                model="gpt-5-mini-2025-08-07",
                prompt="classifier@1",
                input_tokens=612,
                cached_tokens=0,
                output_tokens=210,
                reasoning_tokens=192,
                latency_ms=2890,
                cost=Decimal("0.000573"),
            )
        ],
        "model": "gpt-5-mini-2025-08-07",
        "latency_ms": 7400,
        "first_token_ms": 5100,
    }
    return TurnResult(**{**defaults, **overrides})  # type: ignore[arg-type]


async def _rows() -> list[dict[str, object]]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select * from chat_messages order by id")
        return [dict(row) for row in await cur.fetchall()]


async def test_a_session_is_created_once_and_then_found() -> None:
    first = await chat_db.ensure_session(SESSION)
    second = await chat_db.ensure_session(SESSION)

    assert first == second

    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select created_at, last_seen_at from chat_sessions")
        rows = await cur.fetchall()

    assert len(rows) == 1, "the second call found the row rather than adding one"
    assert rows[0]["last_seen_at"] >= rows[0]["created_at"], "seeing it again bumps last_seen_at"


async def test_a_turn_is_two_rows_and_the_answer_points_at_the_question() -> None:
    session_pk = await chat_db.ensure_session(SESSION)
    asked_at = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)

    message_id = await chat_db.insert_turn(
        session_pk, "Does Mihail work with Laravel?", _result(), asked_at
    )

    question, answer = await _rows()
    assert question["role"] == "user"
    assert question["content"] == "Does Mihail work with Laravel?"
    assert question["classification"] == "mihail_related"
    assert question["created_at"] == asked_at, "recorded when it was asked, not when answered"

    assert answer["id"] == message_id
    assert answer["reply_to_id"] == question["id"]
    assert answer["role"] == "assistant"
    assert answer["top_score"] == pytest.approx(0.62)
    assert answer["fallback_used"] is False
    assert answer["retrieved_chunk_ids"] == [7, 8]
    assert answer["prompt_tokens"] == 612
    assert answer["cost_usd"] == Decimal("0.000573")
    assert answer["first_token_ms"] == 5100


async def test_the_searches_and_the_calls_are_stored_as_json() -> None:
    session_pk = await chat_db.ensure_session(SESSION)

    await chat_db.insert_turn(session_pk, "Laravel?", _result(), dt.datetime.now(dt.UTC))

    _, answer = await _rows()
    tool_calls = answer["tool_calls"]
    llm_calls = answer["llm_calls"]

    assert isinstance(tool_calls, list)
    assert isinstance(llm_calls, list)
    assert tool_calls[0]["query"] == "Laravel PHP experience"
    assert tool_calls[0]["chunk_ids"] == [7, 8]
    assert llm_calls[0]["step"] == "classify"
    assert llm_calls[0]["prompt"] == "classifier@1"
    # Cost as a string inside JSON: a float would round, and these get summed.
    assert llm_calls[0]["cost_usd"] == "0.000573"


async def test_history_comes_back_oldest_first_and_only_as_far_as_the_window() -> None:
    session_pk = await chat_db.ensure_session(SESSION)
    for number in range(3):
        await chat_db.insert_turn(
            session_pk,
            f"Question {number}",
            _result(reply=f"Answer {number}"),
            dt.datetime.now(dt.UTC),
        )

    everything = await chat_db.load_recent_messages(session_pk, limit=50)
    assert [message.content for message in everything] == [
        "Question 0",
        "Answer 0",
        "Question 1",
        "Answer 1",
        "Question 2",
        "Answer 2",
    ]

    window = await chat_db.load_recent_messages(session_pk, limit=2)
    assert [message.content for message in window] == ["Question 2", "Answer 2"]
    assert await chat_db.load_recent_messages(session_pk, limit=0) == []


async def test_deleting_a_session_takes_its_messages_with_it() -> None:
    session_pk = await chat_db.ensure_session(SESSION)
    await chat_db.insert_turn(session_pk, "Laravel?", _result(), dt.datetime.now(dt.UTC))

    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_sessions where id = %s", (session_pk,))

    assert await _rows() == []
