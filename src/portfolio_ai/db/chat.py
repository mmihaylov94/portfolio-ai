"""Reading and writing the chat tables. Every statement that touches them is here.

Two rows per turn -- the question and the answer -- written together in one
transaction, after the answer is complete. Writing the question first, as it
arrives, would be the obvious alternative and is worse: a failure halfway through
would leave a question with no answer, and the next turn would show the model two
questions in a row with nothing between them.

The answer row carries everything about the turn: what it cost, what it retrieved,
what it scored, and ``reply_to_id`` pointing back at the question. The question row
carries the classification as well, so "what do people ask about" is one query
against ``role = 'user'`` rather than a join.

The API's limits are read from here too -- how many questions a conversation asked
recently, what visitors spent today -- and so is the retention purge, which deletes
what is older than the privacy policy allows.
"""

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal

import structlog
from psycopg.types.json import Jsonb

from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.db.pool import get_pool

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ClientInfo:
    """Where a conversation came from, as the proxy in front of the API reported it.

    ``ip_hash`` arrives already hashed (api/security.py). This module never sees an
    address at all, which is the strongest version of "never store one": a layer that
    is never handed a raw IP cannot write one down by accident.
    """

    ip_hash: str | None = None
    user_agent: str | None = None
    referrer: str | None = None


@dataclass(frozen=True)
class RecentQuestions:
    """How much of its rate limit a conversation has used."""

    count: int
    # Seconds until the oldest question in the window drops out of it, freeing a
    # slot: what the API sends as Retry-After once the limit is reached.
    retry_after: int


@dataclass(frozen=True)
class Purged:
    """What a retention sweep removed, or would remove."""

    messages: int
    sessions: int


async def ensure_session(session_id: str, client: ClientInfo | None = None) -> int:
    """The row id for this session, creating the row the first time it is seen.

    ``session_id`` is the opaque string the browser keeps in ``localStorage``; the
    id returned is this table's own, which everything else references.

    ``do update set last_seen_at = now()`` rather than ``do nothing``, because the
    row has to come back either way: ``do nothing`` returns no row at all for a
    session that already exists, while ``do update`` always returns it. The touch is
    wanted anyway -- it is what makes "sessions active this week" a column rather
    than a query over messages.

    ``client`` is written by the insert and never by the update, so it records where
    the conversation *started*. A later request from a different network does not
    rewrite that, and nothing a caller sends mid-conversation can.
    """
    client = client or ClientInfo()
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            insert into chat_sessions (session_id, client_ip_hash, user_agent, referrer)
            values (%(session_id)s, %(ip_hash)s, %(user_agent)s, %(referrer)s)
            on conflict (session_id) do update set last_seen_at = now()
            returning id
            """,
            {
                "session_id": session_id,
                "ip_hash": client.ip_hash,
                "user_agent": client.user_agent,
                "referrer": client.referrer,
            },
        )
        row = await cur.fetchone()

    if row is None:  # pragma: no cover - the upsert above always returns a row
        raise RuntimeError(f"session {session_id!r} was neither inserted nor found")

    return int(row["id"])


async def recent_questions(session_id: str, *, window: dt.timedelta, limit: int) -> RecentQuestions:
    """How many questions this conversation asked in the last ``window``, up to ``limit``.

    Counted from the stored questions, so it survives a restart and needs nothing
    beyond the table that is already there. The question being asked right now is
    not stored yet and so is not counted; the API allows only one in flight per
    conversation, so that is at most one.

    Only the ``limit`` most recent are looked at, because that is all the answer
    needs: when the limit is reached, the oldest of those is the one whose expiry
    frees the next slot, and ``retry_after`` is how long that takes.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            with recent as (
                select m.created_at
                from chat_messages m
                join chat_sessions s on s.id = m.session_id
                where s.session_id = %(session_id)s
                  and m.role = 'user'
                  and m.created_at > now() - %(window)s
                order by m.created_at desc
                limit %(limit)s
            )
            select count(*) as used,
                   extract(epoch from min(created_at) + %(window)s - now()) as seconds_left
            from recent
            """,
            {"session_id": session_id, "window": window, "limit": limit},
        )
        row = await cur.fetchone()

    if row is None:  # pragma: no cover - an aggregate without group by returns one row
        return RecentQuestions(count=0, retry_after=0)

    # min() over no rows is null, and so is everything computed from it.
    seconds = row["seconds_left"]
    return RecentQuestions(
        count=int(row["used"]),
        retry_after=max(1, math.ceil(seconds)) if seconds is not None else 0,
    )


async def spend_since(window: dt.timedelta) -> Decimal:
    """What answering visitors cost over the last ``window``, in dollars.

    The answer rows carry each turn's whole cost, embeddings included, so this is
    one sum. Answers from the eval harness never reach this table and so never
    count against the visitors' budget.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select coalesce(sum(cost_usd), 0) as spent
            from chat_messages
            where role = 'assistant' and created_at > now() - %(window)s
            """,
            {"window": window},
        )
        row = await cur.fetchone()

    return Decimal(row["spent"]) if row else Decimal(0)


async def load_recent_messages(session_pk: int, *, limit: int) -> list[HistoryMessage]:
    """The last ``limit`` messages of a session, oldest first.

    Newest-first in SQL and reversed here, because "the last 50" is what an index
    can answer cheaply and "the last 50, in order" is not.
    """
    if limit <= 0:
        return []

    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select role, content from chat_messages
            where session_id = %s
            order by id desc
            limit %s
            """,
            (session_pk, limit),
        )
        rows = await cur.fetchall()

    return [HistoryMessage(role=row["role"], content=row["content"]) for row in reversed(rows)]


async def insert_turn(
    session_pk: int, question: str, result: TurnResult, asked_at: dt.datetime
) -> int:
    """Store one exchange. Returns the answer's id, which feedback is attached to.

    ``asked_at`` is when the visitor sent the message, not when this runs -- an
    answer can take ten seconds, and "when was this asked" is the question the
    analytics ask. The answer row keeps the default of now().
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            insert into chat_messages (session_id, role, content, classification, created_at)
            values (%s, 'user', %s, %s, %s)
            returning id
            """,
            (session_pk, question, result.classification, asked_at),
        )
        question_row = await cur.fetchone()
        question_id = int(question_row["id"]) if question_row else None

        await cur.execute(
            """
            insert into chat_messages (
                session_id, role, content, reply_to_id, tool_calls, retrieved_chunk_ids,
                classification, top_score, fallback_used, model, prompt_tokens,
                completion_tokens, cost_usd, latency_ms, first_token_ms, llm_calls
            )
            values (%s, 'assistant', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            returning id
            """,
            (
                session_pk,
                result.reply,
                question_id,
                # Jsonb, not a string: psycopg then sends it as jsonb rather than
                # as text that happens to contain JSON, and the column would reject
                # the latter.
                Jsonb([search.as_record() for search in result.searches]),
                result.retrieved_chunk_ids,
                result.classification,
                result.top_score,
                result.fallback_used,
                result.model,
                result.prompt_tokens,
                result.completion_tokens,
                result.cost,
                result.latency_ms,
                result.first_token_ms,
                Jsonb([call.as_record() for call in result.calls]),
            ),
        )
        answer_row = await cur.fetchone()

    if answer_row is None:  # pragma: no cover - an insert ... returning always returns
        raise RuntimeError("the answer was inserted but its id did not come back")

    return int(answer_row["id"])


async def count_older_than(cutoff: dt.datetime) -> Purged:
    """What :func:`delete_older_than` would remove, without removing it."""
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select
                (select count(*) from chat_messages where created_at < %(cutoff)s) as messages,
                (select count(*) from chat_sessions where last_seen_at < %(cutoff)s) as sessions
            """,
            {"cutoff": cutoff},
        )
        row = await cur.fetchone()

    return (
        Purged(messages=int(row["messages"]), sessions=int(row["sessions"]))
        if row
        else Purged(0, 0)
    )


async def delete_older_than(cutoff: dt.datetime) -> Purged:
    """Delete messages from before ``cutoff``, and sessions last seen before it.

    Two statements, one transaction. Messages first, by their own age -- a
    conversation that is still going keeps its recent turns and loses its old ones.
    Then sessions nobody has come back to, which carry the hashed address and the
    browser string.

    The foreign keys do the rest, and each was chosen with this sweep in mind:

    - ``message_feedback`` cascades, so a rating goes with the answer it rated.
    - ``reply_to_id`` cascades, so an answer goes with its question even when it
      was written a few seconds on the right side of the cutoff. Half a turn is not
      worth keeping.
    - ``eval_cases.source_message_id`` is ``on delete set null``, so a question
      promoted into the eval dataset survives the conversation it came from.

    The counts are the rows each statement matched; rows removed by a cascade are
    not in them.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            "delete from chat_messages where created_at < %(cutoff)s", {"cutoff": cutoff}
        )
        messages = cur.rowcount
        await cur.execute(
            "delete from chat_sessions where last_seen_at < %(cutoff)s", {"cutoff": cutoff}
        )
        sessions = cur.rowcount

    return Purged(messages=messages, sessions=sessions)
