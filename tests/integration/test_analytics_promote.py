"""Promoting a stored question, against a real Postgres.

What only a database can show: that an id finds its turn whether it is the
question's or the answer's, and that "the conversation before the question" is the
right conversation, the right side of the question, and in the order it happened.
"""

import datetime as dt

import corpus
import pytest

from portfolio_ai.analytics import promote
from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.evals.datasets import Turn
from portfolio_ai.exceptions import AnalyticsError

pytestmark = pytest.mark.integration

VISITOR = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER = "bbbbbbbb-1111-4222-8333-444444444444"
NOON = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)
MINUTE = dt.timedelta(minutes=1)


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()


async def _ask(  # ruff: ignore[too-many-arguments] -- one turn's worth, all named
    question: str,
    answer: str = "An answer.",
    *,
    visitor: str = VISITOR,
    minutes: float = 0,
    route: str = "mihail_related",
    fallback: bool = False,
) -> int:
    """Store one answered question. Returns the question's id; its answer's is one more."""
    result = TurnResult(
        reply=answer,
        classification=route,  # type: ignore[arg-type]
        citations=[],
        searches=[],
        retrieved_chunk_ids=[],
        top_score=0.3 if route == "mihail_related" else None,
        fallback_used=fallback,
        links_removed=0,
        calls=[],
        model="a-model",
        latency_ms=4000,
        first_token_ms=3000,
    )
    session_pk = await chat_db.ensure_session(visitor)
    answer_id = await chat_db.insert_turn(session_pk, question, result, NOON + minutes * MINUTE)
    return answer_id - 1


# --- finding the question -------------------------------------------------------------


async def test_a_question_is_found_by_its_own_id_and_by_its_answers() -> None:
    await _ask("Hello", route="small_talk")
    question_id = await _ask("What's his day rate?", minutes=1, fallback=True)

    by_question = await analytics_db.answered_question(question_id)
    by_answer = await analytics_db.answered_question(question_id + 1)

    assert by_question is not None
    assert by_question == by_answer
    assert (by_question.question_id, by_question.question) == (question_id, "What's his day rate?")
    assert (by_question.route, by_question.fallback_used) == ("mihail_related", True)
    assert by_question.asked_at == NOON + MINUTE


async def test_what_happened_to_a_question_is_read_from_its_answer() -> None:
    """Both rows of a turn store the route, and store the same one. The question's
    is changed here so that the two differ, which they never do otherwise: read from
    the wrong row, this test would say so."""
    refused = await _ask("What is Glotsmith?", route="out_of_scope")
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(
            "update chat_messages set classification = 'small_talk' where id = %s", (refused,)
        )

    found = await analytics_db.answered_question(refused)

    assert found is not None
    assert (found.route, found.fallback_used) == ("out_of_scope", False)


async def test_an_id_that_names_no_message_finds_nothing() -> None:
    question_id = await _ask("What's his day rate?")

    assert await analytics_db.answered_question(question_id + 1000) is None


# --- the conversation before it -------------------------------------------------------


async def test_the_exchanges_before_a_question_are_its_own_conversations_oldest_first() -> None:
    first = await _ask("What does he do?", "He is a Solutions Architect.")
    await _ask("Somebody else's question?", "Somebody else's answer.", visitor=OTHER, minutes=1)
    await _ask("Does he consult?", "Yes, he does.", minutes=2)
    third = await _ask("What's his day rate?", "That is not listed.", minutes=3)
    await _ask("And for a week?", "Not listed either.", minutes=4)
    asked = await analytics_db.answered_question(third)
    assert asked is not None

    before = await analytics_db.exchanges_before(asked.session_pk, third, limit=25)

    assert before == [
        ("What does he do?", "He is a Solutions Architect."),
        ("Does he consult?", "Yes, he does."),
    ], "not the other visitor's, not the question itself, and not what came after it"
    assert await analytics_db.exchanges_before(asked.session_pk, first, limit=25) == []


async def test_a_limit_keeps_the_exchanges_nearest_the_question() -> None:
    """The assistant remembers the last so many, so those are the ones it was given."""
    await _ask("One?", "First.")
    await _ask("Two?", "Second.", minutes=1)
    await _ask("Three?", "Third.", minutes=2)
    fourth = await _ask("Four?", "Fourth.", minutes=3)
    asked = await analytics_db.answered_question(fourth)
    assert asked is not None

    assert await analytics_db.exchanges_before(asked.session_pk, fourth, limit=2) == [
        ("Two?", "Second."),
        ("Three?", "Third."),
    ]
    assert await analytics_db.exchanges_before(asked.session_pk, fourth, limit=0) == []


# --- the whole thing --------------------------------------------------------------------


async def test_a_stored_question_is_promoted_with_its_history_and_printed_as_a_case() -> None:
    await corpus.seed("services", "Services", [("Rates", "A day costs this.", corpus.axis(0))])
    await _ask("Does he consult?", "Yes, he does. Want to know how?")
    question_id = await _ask(
        "What's his day rate?", "That is not listed.", minutes=1, fallback=True
    )

    case = await promote.promote(
        question_id + 1, expected_docs=["services"], key="day-rate", with_history=True
    )

    assert (case.key, case.question, case.category) == (
        "day-rate",
        "What's his day rate?",
        "mihail_related",
    )
    assert case.expected_doc_ids == ("services",)
    assert case.history == (
        Turn(role="user", content="Does he consult?"),
        Turn(role="assistant", content="Yes, he does. Want to know how?"),
    )
    assert case.notes == (
        f"Promoted from a visitor's question (message {question_id}, asked 2026-10-12). "
        "It was routed as mihail_related and answered with the fallback."
    )
    printed = promote.as_yaml(case)
    assert printed.startswith("  - key: day-rate\n    question: What's his day rate?\n")


async def test_promoting_against_an_article_that_is_not_indexed_is_refused() -> None:
    question_id = await _ask("What's his day rate?")

    with pytest.raises(AnalyticsError, match="No indexed article has the doc_id 'services'"):
        await promote.promote(question_id, expected_docs=["services"], key=None, with_history=False)
