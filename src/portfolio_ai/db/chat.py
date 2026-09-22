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
"""

import datetime as dt

import structlog
from psycopg.types.json import Jsonb

from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.db.pool import get_pool

log = structlog.get_logger(__name__)


async def ensure_session(session_id: str) -> int:
    """The row id for this session, creating the row the first time it is seen.

    ``session_id`` is the opaque string the browser keeps in ``localStorage``; the
    id returned is this table's own, which everything else references.

    ``do update set last_seen_at = now()`` rather than ``do nothing``, because the
    row has to come back either way: ``do nothing`` returns no row at all for a
    session that already exists, while ``do update`` always returns it. The touch is
    wanted anyway -- it is what makes "sessions active this week" a column rather
    than a query over messages.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            insert into chat_sessions (session_id) values (%s)
            on conflict (session_id) do update set last_seen_at = now()
            returning id
            """,
            (session_id,),
        )
        row = await cur.fetchone()

    if row is None:  # pragma: no cover - the upsert above always returns a row
        raise RuntimeError(f"session {session_id!r} was neither inserted nor found")

    return int(row["id"])


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
