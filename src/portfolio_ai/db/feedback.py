"""Thumbs up and down on answers. Every statement that touches ``message_feedback``.

Explicit feedback is the weakest signal the analytics have -- few visitors click,
and those who do lean negative -- but it is the only one that says in the visitor's
own words that an answer was wrong. Worth capturing for that alone.
"""

from portfolio_ai.db.pool import get_pool


async def record_feedback(
    *, session_id: str, message_id: int, rating: int, comment: str | None
) -> bool:
    """Store a rating for an answer. False if that answer is not in this conversation.

    One statement does both the check and the write. The ``select`` finds the answer
    only when it is an answer (``role = 'assistant'``) and belongs to the session the
    caller named -- so nobody can rate another visitor's conversation by guessing ids,
    and nothing can rate a question. If it finds nothing, nothing is inserted,
    nothing comes back, and the caller says 404.

    Doing it as a check and then a write would work too, with a gap between them in
    which the message could be deleted by the retention sweep. One statement has no
    gap, and is one round trip instead of two.

    Voting again replaces the vote: ``on conflict`` on the unique ``message_id``
    turns the insert into an update. ``created_at`` keeps the first vote's time.

    The casts are there because parameters in a ``select`` list are not inferred
    from the table they end up in, the way ``values (...)`` would infer them.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            insert into message_feedback (message_id, rating, comment)
            select m.id, %(rating)s::smallint, %(comment)s::text
            from chat_messages m
            join chat_sessions s on s.id = m.session_id
            where m.id = %(message_id)s
              and m.role = 'assistant'
              and s.session_id = %(session_id)s
            on conflict (message_id) do update
                set rating = excluded.rating, comment = excluded.comment
            returning id
            """,
            {
                "session_id": session_id,
                "message_id": message_id,
                "rating": rating,
                "comment": comment,
            },
        )
        row = await cur.fetchone()

    return row is not None
