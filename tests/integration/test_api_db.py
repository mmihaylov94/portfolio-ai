"""The SQL behind the API's limits, feedback and retention, against a real Postgres.

These are the queries a unit test cannot check: a window measured by the database's
own clock, an upsert whose ``select`` quietly matches nothing, and foreign keys that
decide what a retention sweep takes with it.
"""

import datetime as dt
from decimal import Decimal

import corpus
import pytest

from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db import feedback as feedback_db
from portfolio_ai.db.chat import ClientInfo
from portfolio_ai.db.pool import get_pool

pytestmark = pytest.mark.integration

SESSION = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER_SESSION = "bbbbbbbb-1111-4222-8333-444444444444"
WINDOW = dt.timedelta(minutes=15)


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from eval_datasets")


def _result(reply: str = "Yes.") -> TurnResult:
    return TurnResult(
        reply=reply,
        classification="mihail_related",
        citations=[],
        searches=[],
        retrieved_chunk_ids=[],
        top_score=0.6,
        fallback_used=False,
        links_removed=0,
        calls=[],
        model="gpt-5-mini",
        latency_ms=100,
        first_token_ms=50,
    )


async def _turn(session_id: str, *, asked_ago: dt.timedelta = dt.timedelta(0)) -> int:
    """Store one turn, as if asked ``asked_ago``. Returns the answer's id."""
    session_pk = await chat_db.ensure_session(session_id)
    asked_at = dt.datetime.now(dt.UTC) - asked_ago
    answer_id = await chat_db.insert_turn(session_pk, "A question?", _result(), asked_at)
    # insert_turn stamps the answer with now(); age it to match its question.
    await _execute("update chat_messages set created_at = %s where id = %s", (asked_at, answer_id))
    return answer_id


async def _execute(sql: str, params: tuple[object, ...] = ()) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(sql, params)


async def _one(sql: str, params: tuple[object, ...] = ()) -> dict[str, object]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        row = await cur.fetchone()
    assert row is not None
    return dict(row)


# --- where a conversation came from ---------------------------------------------


async def test_the_visitor_is_recorded_on_the_first_request_only() -> None:
    first = ClientInfo(ip_hash="a" * 64, user_agent="Firefox", referrer="https://mihaylov.io/")
    later = ClientInfo(ip_hash="b" * 64, user_agent="Chrome", referrer="https://mihaylov.io/x")

    session_pk = await chat_db.ensure_session(SESSION, first)
    assert await chat_db.ensure_session(SESSION, later) == session_pk

    row = await _one(
        "select client_ip_hash, user_agent, referrer from chat_sessions where id = %s",
        (session_pk,),
    )
    assert row == {
        "client_ip_hash": "a" * 64,
        "user_agent": "Firefox",
        "referrer": "https://mihaylov.io/",
    }


# --- the per-conversation limit -------------------------------------------------


async def test_only_this_conversations_recent_questions_count() -> None:
    await _turn(SESSION)
    await _turn(SESSION, asked_ago=dt.timedelta(minutes=5))
    await _turn(SESSION, asked_ago=dt.timedelta(minutes=20))  # outside the window
    await _turn(OTHER_SESSION)

    recent = await chat_db.recent_questions(SESSION, window=WINDOW, limit=20)

    assert recent.count == 2, "answers are not questions, and old or other ones do not count"


async def test_at_the_limit_retry_after_is_when_the_oldest_leaves_the_window() -> None:
    await _turn(SESSION, asked_ago=dt.timedelta(minutes=10))  # leaves the window in ~5 min
    await _turn(SESSION, asked_ago=dt.timedelta(minutes=2))

    recent = await chat_db.recent_questions(SESSION, window=WINDOW, limit=2)

    assert recent.count == 2
    assert 295 <= recent.retry_after <= 301


async def test_only_the_last_limit_questions_decide_the_wait() -> None:
    """With more questions in the window than the limit, the one that frees a slot
    is the limit-th most recent, not the oldest overall."""
    for minutes in (14, 12, 3, 1):
        await _turn(SESSION, asked_ago=dt.timedelta(minutes=minutes))

    recent = await chat_db.recent_questions(SESSION, window=WINDOW, limit=2)

    assert recent.count == 2
    assert 11 * 60 - 1 <= recent.retry_after <= 12 * 60 + 1, "the one from 3 minutes ago"


async def test_a_conversation_nobody_has_seen_has_used_nothing() -> None:
    recent = await chat_db.recent_questions("never-seen-before", window=WINDOW, limit=20)

    assert (recent.count, recent.retry_after) == (0, 0)


# --- the spending limit -----------------------------------------------------------


async def test_spend_is_the_answers_of_the_last_day() -> None:
    first = await _turn(SESSION)
    second = await _turn(OTHER_SESSION, asked_ago=dt.timedelta(hours=2))
    old = await _turn(SESSION, asked_ago=dt.timedelta(hours=30))
    for answer_id, cost in ((first, "0.004"), (second, "0.0025"), (old, "5")):
        await _execute("update chat_messages set cost_usd = %s where id = %s", (cost, answer_id))

    spent = await chat_db.spend_since(dt.timedelta(hours=24))

    assert spent == Decimal("0.0065")


async def test_no_answers_means_nothing_spent() -> None:
    assert await chat_db.spend_since(dt.timedelta(hours=24)) == Decimal(0)


# --- feedback ---------------------------------------------------------------------


async def test_a_vote_is_stored_and_voting_again_replaces_it() -> None:
    answer_id = await _turn(SESSION)

    assert await feedback_db.record_feedback(
        session_id=SESSION, message_id=answer_id, rating=1, comment=None
    )
    assert await feedback_db.record_feedback(
        session_id=SESSION, message_id=answer_id, rating=-1, comment="Actually, no."
    )

    row = await _one(
        "select count(*) as votes, min(rating) as rating, min(comment) as comment "
        "from message_feedback where message_id = %s",
        (answer_id,),
    )
    assert row == {"votes": 1, "rating": -1, "comment": "Actually, no."}


async def test_a_vote_on_someone_elses_answer_stores_nothing() -> None:
    answer_id = await _turn(SESSION)

    stored = await feedback_db.record_feedback(
        session_id=OTHER_SESSION, message_id=answer_id, rating=-1, comment=None
    )

    assert stored is False
    assert (await _one("select count(*) as n from message_feedback"))["n"] == 0


async def test_a_question_cannot_be_rated_and_neither_can_nothing() -> None:
    answer_id = await _turn(SESSION)
    question_id = answer_id - 1  # inserted just before its answer, in the same transaction

    for message_id in (question_id, answer_id + 1000):
        assert not await feedback_db.record_feedback(
            session_id=SESSION, message_id=message_id, rating=1, comment=None
        )


# --- retention ----------------------------------------------------------------------


async def test_the_sweep_removes_what_is_old_and_keeps_what_is_not() -> None:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=90)

    old_answer = await _turn(SESSION, asked_ago=dt.timedelta(days=100))
    recent_answer = await _turn(SESSION, asked_ago=dt.timedelta(days=1))
    abandoned = await _turn(OTHER_SESSION, asked_ago=dt.timedelta(days=95))
    await _execute(
        "update chat_sessions set last_seen_at = %s where session_id = %s",
        (dt.datetime.now(dt.UTC) - dt.timedelta(days=95), OTHER_SESSION),
    )
    # A rating on the old answer, and the old question promoted into the eval set.
    await feedback_db.record_feedback(
        session_id=SESSION, message_id=old_answer, rating=-1, comment=None
    )
    dataset = await _one("insert into eval_datasets (name) values ('golden') returning id")
    await _execute(
        "insert into eval_cases (dataset_id, key, question, category, source_message_id) "
        "values (%s, 'promoted', 'A question?', 'mihail_related', %s)",
        (dataset["id"], old_answer - 1),
    )

    planned = await chat_db.count_older_than(cutoff)
    purged = await chat_db.delete_older_than(cutoff)

    assert planned == purged, "the dry run counts exactly what the sweep deletes"
    assert (purged.messages, purged.sessions) == (4, 1)

    remaining = await _one("select array_agg(id order by id) as ids from chat_messages")
    assert remaining["ids"] == [recent_answer - 1, recent_answer]
    assert (await _one("select count(*) as n from message_feedback"))["n"] == 0
    assert (await _one("select count(*) as n from chat_sessions"))["n"] == 1
    survivor = await _one("select question, source_message_id from eval_cases")
    assert survivor == {"question": "A question?", "source_message_id": None}
    assert abandoned not in remaining["ids"]
