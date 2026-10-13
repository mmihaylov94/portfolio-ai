"""Reading the chat tables back, for the analytics. Every statement of that kind is here.

``db/chat.py`` writes a conversation as it happens and reads what the next answer
needs. This module is the other direction: long after the answer, what was asked,
how it went, and which articles the answers drew on.

Three things about the queries are worth knowing before reading them.

**A window is measured on the question's time**, never the answer's. The two are
seconds apart, and an analysis of "last week" should not split a turn across its
edge.

**A turn is found from its answer**, through ``reply_to_id``. The answer row is the
one that carries the score, the fallback flag, the cost and the timings; the
question row carries the visitor's words and when they were sent.

**A window is half-open**: from ``since`` up to, but not including, ``until``. Two
windows that share an edge then never count one question twice, which is what lets
"this week against the week before" be two calls with one timestamp in common.

The last three functions belong to ``analytics seed``, which puts invented
conversations into the development database and takes them out again.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from portfolio_ai.db.pool import get_pool


@dataclass(frozen=True)
class Turn:
    """One question and the answer it got, as the analytics read them."""

    question_id: int
    answer_id: int
    # chat_sessions.id, this database's own number for the conversation. Never the
    # browser's string: nothing here needs to know which browser it was.
    conversation: int
    asked_at: dt.datetime
    question: str
    answer: str
    # The classifier's choice: mihail_related, small_talk or out_of_scope.
    route: str | None
    # None for every answer that did not search.
    top_score: float | None
    fallback_used: bool
    # A thumbs up (1) or down (-1), if the visitor left one, and what they wrote.
    rating: int | None
    comment: str | None
    cost: Decimal
    latency_ms: int | None
    first_token_ms: int | None


@dataclass(frozen=True)
class Volume:
    """How much was asked in a window."""

    questions: int
    conversations: int


@dataclass(frozen=True)
class ArticleUse:
    """How often one article was among an answer's best passages."""

    doc_id: str
    title: str
    sections: int
    # The headings of the sections that were among no answer's best passages, in
    # the article's own order.
    unused: list[str]
    answers: int

    @property
    def sections_used(self) -> int:
        return self.sections - len(self.unused)


@dataclass(frozen=True)
class ArticleUsage:
    # Answers in the window that searched at all.
    searched: int
    # Of those, the ones that recorded which article each passage came from. An
    # answer stored by the code as it was before 2026-10-13 recorded only chunk ids,
    # which stop meaning anything the next time their article is edited. Those are
    # left out of every count below, instead of being credited to whichever
    # articles happen not to have changed since.
    counted: int
    articles: list[ArticleUse]


async def turns_between(since: dt.datetime, until: dt.datetime) -> list[Turn]:
    """Every answered question asked in the window, one conversation after another.

    Ordered by conversation and then by time, which is the order the asked-again
    check reads them in: it compares each question with the one before it.

    ``left join`` on the feedback, because most answers have none and must still be
    here; an ordinary ``join`` would return only the answers somebody rated.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select q.id as question_id, a.id as answer_id, q.session_id as conversation,
                   q.created_at as asked_at, q.content as question, a.content as answer,
                   a.classification, a.top_score, a.fallback_used,
                   f.rating, f.comment,
                   coalesce(a.cost_usd, 0) as cost, a.latency_ms, a.first_token_ms
            from chat_messages a
            join chat_messages q on q.id = a.reply_to_id
            left join message_feedback f on f.message_id = a.id
            where a.role = 'assistant'
              and q.created_at >= %(since)s
              and q.created_at < %(until)s
            order by q.session_id, q.created_at, q.id
            """,
            {"since": since, "until": until},
        )
        rows = await cur.fetchall()

    return [
        Turn(
            question_id=row["question_id"],
            answer_id=row["answer_id"],
            conversation=row["conversation"],
            asked_at=row["asked_at"],
            question=row["question"],
            answer=row["answer"],
            route=row["classification"],
            top_score=row["top_score"],
            fallback_used=row["fallback_used"],
            rating=row["rating"],
            comment=row["comment"],
            cost=Decimal(row["cost"]),
            latency_ms=row["latency_ms"],
            first_token_ms=row["first_token_ms"],
        )
        for row in rows
    ]


async def volume_between(since: dt.datetime, until: dt.datetime) -> Volume:
    """How many questions were asked in the window, in how many conversations.

    For the window before the one being reported, where the counts are all that is
    wanted and fetching every row to count them would be a waste.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select count(*) as questions, count(distinct session_id) as conversations
            from chat_messages
            where role = 'user'
              and created_at >= %(since)s
              and created_at < %(until)s
            """,
            {"since": since, "until": until},
        )
        row = await cur.fetchone()

    if row is None:  # pragma: no cover - an aggregate without group by returns one row
        return Volume(questions=0, conversations=0)
    return Volume(questions=int(row["questions"]), conversations=int(row["conversations"]))


# The first search of each answer in the window: one row per answer.
#
# An answer's searches are stored as JSON on its row (`tool_calls`), each with three
# lists in the same order, best passage first: the chunk ids, the doc_id of the
# article each came from, and the section's slug. `-> 0` takes the first search.
# The first and not all of them, because the first is the one every knowledge-base
# answer makes; a later one returns only what the first had not.
#
# `doc_ids` and `sections` are null for an answer stored before they were recorded.
_SEARCHED = """
    with searched as (
        select a.id as answer_id,
               a.tool_calls -> 0 -> 'doc_ids' as doc_ids,
               a.tool_calls -> 0 -> 'sections' as sections
        from chat_messages a
        join chat_messages q on q.id = a.reply_to_id
        where a.role = 'assistant'
          and q.created_at >= %(since)s
          and q.created_at < %(until)s
          and jsonb_typeof(a.tool_calls -> 0 -> 'chunk_ids') = 'array'
    )
"""

_ANSWERS_COUNTED = """
    select count(*) as searched,
           count(*) filter (where jsonb_typeof(doc_ids) = 'array') as counted
    from searched
"""

# The best passages of those searches: one row per passage, with its article and
# section.
#
# `jsonb_array_elements_text` turns a JSON list into rows. `rows from (f, g)` runs
# two such functions side by side, so the first doc_id sits beside the first
# section, the second beside the second. `with ordinality` numbers the rows as they
# come out, and since the lists are stored best first that number is the passage's
# rank: `position <= %(best)s` keeps the top few.
#
# A function in a `from` clause may read the columns of the tables before it, which
# is how these read `s.doc_ids`. (A subquery in the same place may not, unless it is
# written `lateral`.) Handed a null, they return no rows, so an answer that recorded
# no doc_ids simply contributes nothing.
_BEST = """
    , best as (
        select s.answer_id, item.doc_id, item.section
        from searched s
        cross join rows from (
            jsonb_array_elements_text(s.doc_ids),
            jsonb_array_elements_text(s.sections)
        ) with ordinality as item(doc_id, section, position)
        where item.position <= %(best)s
    )
"""

# Every article, with how many answers drew on it and which of its sections none
# did. Starting from `documents` is what lists the articles no answer touched.
#
# An article is matched by doc_id and a section by its slug as well, and neither
# changes when the article is indexed again. `is not distinct from` is `=` that
# treats two nulls as equal: an article with no headings has one section and no
# slug for it.
_ANSWERS_BY_ARTICLE = """
    select d.doc_id, d.title,
           (select count(*) from chunks c where c.document_id = d.id) as sections,
           (select count(distinct b.answer_id) from best b where b.doc_id = d.doc_id) as answers,
           array(
               select coalesce(c.section_title, c.section, 'main')
               from chunks c
               where c.document_id = d.id
                 and not exists (
                     select 1
                     from best b
                     where b.doc_id = d.doc_id
                       and b.section is not distinct from c.section
                 )
               order by c.chunk_index
           ) as unused
    from documents d
    order by answers desc, d.doc_id
"""


async def article_usage(since: dt.datetime, until: dt.datetime, *, best: int) -> ArticleUsage:
    """Which articles the window's answers drew on, most used first.

    "Drew on" means: one of its sections was among the ``best`` highest-scoring
    passages of the answer's first search. Not "was retrieved": a search returns
    twenty passages out of a hundred and twenty, so over a few dozen answers nearly
    every section is retrieved by something, and the count says nothing.

    Counted by doc_id and section slug, which each answer records, and not by chunk
    id. A chunk id changes every time its article is indexed again. Counting by it
    made an article look unused the day after it was edited, because every earlier
    answer that drew on it pointed at ids that were gone.

    The statements are fixed texts joined: nothing is put into them but parameters.
    """
    window = {"since": since, "until": until}
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SEARCHED + _ANSWERS_COUNTED, window)
        totals = await cur.fetchone()

        await cur.execute(_SEARCHED + _BEST + _ANSWERS_BY_ARTICLE, {**window, "best": best})
        rows = await cur.fetchall()

    return ArticleUsage(
        searched=int(totals["searched"]) if totals else 0,
        counted=int(totals["counted"]) if totals else 0,
        articles=[
            ArticleUse(
                doc_id=row["doc_id"],
                title=row["title"],
                sections=int(row["sections"]),
                unused=list(row["unused"]),
                answers=int(row["answers"]),
            )
            for row in rows
        ],
    )


# --- invented conversations (analytics seed) ------------------------------------


def _checked(prefix: str) -> str:
    # starts_with(anything, '') is true, so an empty prefix would count, or delete,
    # every conversation there is.
    if not prefix:
        raise ValueError("a session prefix cannot be empty")
    return prefix


async def count_conversations(prefix: str) -> int:
    """How many stored conversations have a browser id starting with ``prefix``."""
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "select count(*) as found from chat_sessions where starts_with(session_id, %s)",
            (_checked(prefix),),
        )
        row = await cur.fetchone()

    return int(row["found"]) if row else 0


async def delete_conversations(prefix: str) -> int:
    """Delete every conversation whose browser id starts with ``prefix``.

    Messages and their feedback go with the session, through the same foreign keys
    the retention sweep relies on. ``starts_with`` rather than ``like``: in a
    ``like`` pattern an underscore matches any character, and a prefix should match
    nothing but itself.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "delete from chat_sessions where starts_with(session_id, %s)", (_checked(prefix),)
        )
        return cur.rowcount


async def backdate_conversation(session_id: str, by: dt.timedelta) -> None:
    """Move one conversation, and everything in it, ``by`` this long into the past.

    The session, its messages and the thumbs left on them. The messages keep their
    order and their distance from each other, so a question asked ten seconds after
    another still is. All three statements run in one transaction: a conversation
    whose session says last week and whose messages say today would be read wrongly
    by everything.
    """
    pool = await get_pool()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        # The feedback first, while the messages it hangs from are still where the
        # join expects them. Not that the join looks at a time: it is the order that
        # reads most plainly, outermost table last.
        await cur.execute(
            """
            update message_feedback f
            set created_at = f.created_at - %(by)s
            from chat_messages m
            join chat_sessions s on s.id = m.session_id
            where m.id = f.message_id and s.session_id = %(session_id)s
            """,
            {"by": by, "session_id": session_id},
        )
        await cur.execute(
            """
            update chat_messages m
            set created_at = m.created_at - %(by)s
            from chat_sessions s
            where s.id = m.session_id and s.session_id = %(session_id)s
            """,
            {"by": by, "session_id": session_id},
        )
        await cur.execute(
            """
            update chat_sessions
            set created_at = created_at - %(by)s, last_seen_at = last_seen_at - %(by)s
            where session_id = %(session_id)s
            """,
            {"by": by, "session_id": session_id},
        )
