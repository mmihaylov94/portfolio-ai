"""The analytics' queries, against a real Postgres.

What only a database can show: a window with an open end, a left join that keeps
the answers nobody rated, a JSON list unpacked into ranked rows, passage ids that
no longer point at anything, and a prefix match that must not treat an underscore
as "any character".
"""

import datetime as dt
from decimal import Decimal

import corpus
import pytest

from portfolio_ai.assistant.agent import Search, TurnResult
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db import feedback as feedback_db
from portfolio_ai.db.pool import get_pool

pytestmark = pytest.mark.integration

VISITOR = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER = "bbbbbbbb-1111-4222-8333-444444444444"

# A fixed moment, so that "exactly on the edge of the window" can be written down.
NOON = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()


def _searched(*passages: tuple[str, str | None]) -> Search:
    """A search that returned these passages, best first, each as (doc_id, section slug).

    The chunk ids are numbers that point at nothing, on purpose: the counts must not
    depend on them.
    """
    return Search(
        query="a query",
        chunk_ids=[900_000 + index for index in range(len(passages))],
        hits=20,
        top_score=0.6,
        duration_ms=30,
        doc_ids=[doc_id for doc_id, _ in passages],
        sections=[section for _, section in passages],
    )


def _result(
    *,
    route: str = "mihail_related",
    top_score: float | None = 0.6,
    fallback: bool = False,
    searches: list[Search] | None = None,
) -> TurnResult:
    return TurnResult(
        reply="An answer.",
        classification=route,  # type: ignore[arg-type]
        citations=[],
        searches=searches or [],
        retrieved_chunk_ids=[],
        top_score=top_score,
        fallback_used=fallback,
        links_removed=0,
        calls=[],
        model="a-model",
        latency_ms=4000,
        first_token_ms=3000,
    )


async def _turn(
    session_id: str, question: str, asked_at: dt.datetime, result: TurnResult | None = None
) -> int:
    """Store one turn asked at ``asked_at``. Returns the answer's id."""
    session_pk = await chat_db.ensure_session(session_id)
    answer_id = await chat_db.insert_turn(session_pk, question, result or _result(), asked_at)
    # insert_turn stamps the answer with now(); a few seconds after its question is
    # where a real one sits.
    await _execute(
        "update chat_messages set created_at = %s where id = %s",
        (asked_at + dt.timedelta(seconds=4), answer_id),
    )
    return answer_id


async def _execute(sql: str, params: tuple[object, ...] = ()) -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute(sql, params)


async def _all(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return [dict(row) for row in await cur.fetchall()]


# --- turns ------------------------------------------------------------------------


async def test_a_turn_comes_back_with_what_its_answer_recorded() -> None:
    answer_id = await _turn(
        VISITOR, "What is his day rate?", NOON, _result(top_score=0.31, fallback=True)
    )
    await _execute("update chat_messages set cost_usd = %s where id = %s", ("0.000650", answer_id))
    await feedback_db.record_feedback(
        session_id=VISITOR, message_id=answer_id, rating=-1, comment="Not an answer."
    )

    [turn] = await analytics_db.turns_between(NOON - HOUR, NOON + HOUR)

    assert (turn.question_id, turn.answer_id) == (answer_id - 1, answer_id)
    assert turn.asked_at == NOON, "the question's time, not the answer's four seconds later"
    assert (turn.question, turn.answer) == ("What is his day rate?", "An answer.")
    assert turn.route == "mihail_related"
    # Exactly. The column is a `real`: unrounded, 0.31 would come back as
    # 0.3100000023841858, and a score of exactly a threshold would read as under it.
    assert turn.top_score == 0.31  # ruff: ignore[float-equality-comparison]
    assert turn.fallback_used is True
    assert (turn.rating, turn.comment) == (-1, "Not an answer.")
    assert turn.cost == Decimal("0.000650")
    assert (turn.latency_ms, turn.first_token_ms) == (4000, 3000)


async def test_an_answer_nobody_rated_is_still_a_turn() -> None:
    """The reason the feedback is a left join. An inner join returns only the
    answers somebody rated, which is almost none of them."""
    await _turn(VISITOR, "A question?", NOON)

    [turn] = await analytics_db.turns_between(NOON - HOUR, NOON + HOUR)

    assert (turn.rating, turn.comment) == (None, None)
    assert turn.cost == Decimal(0), "no cost recorded reads as nothing spent, not as None"


async def test_the_window_is_on_the_question_and_open_at_its_end() -> None:
    """From `since` up to, not including, `until`. Two windows that share an edge
    must not both count the question asked exactly on it."""
    await _turn(VISITOR, "a second early", NOON - dt.timedelta(seconds=1))
    await _turn(VISITOR, "on the start", NOON)
    await _turn(VISITOR, "inside", NOON + dt.timedelta(minutes=30))
    await _turn(VISITOR, "on the end", NOON + HOUR)

    this_hour = await analytics_db.turns_between(NOON, NOON + HOUR)
    next_hour = await analytics_db.turns_between(NOON + HOUR, NOON + 2 * HOUR)

    assert [turn.question for turn in this_hour] == ["on the start", "inside"]
    assert [turn.question for turn in next_hour] == ["on the end"]


async def test_a_question_just_inside_the_window_brings_its_later_answer() -> None:
    """The answer is stamped four seconds after the question here, past `until`.
    The turn belongs to the window its question was asked in."""
    await _turn(VISITOR, "asked at the last moment", NOON + HOUR - dt.timedelta(seconds=1))

    turns = await analytics_db.turns_between(NOON, NOON + HOUR)

    assert [turn.question for turn in turns] == ["asked at the last moment"]


async def test_turns_come_one_conversation_after_another_in_the_order_asked() -> None:
    await _turn(OTHER, "other, first", NOON)
    await _turn(VISITOR, "visitor, second", NOON + dt.timedelta(minutes=2))
    await _turn(VISITOR, "visitor, first", NOON + dt.timedelta(minutes=1))
    await _turn(OTHER, "other, second", NOON + dt.timedelta(minutes=3))

    turns = await analytics_db.turns_between(NOON - HOUR, NOON + HOUR)

    grouped: dict[int, list[str]] = {}
    for turn in turns:
        grouped.setdefault(turn.conversation, []).append(turn.question)
    assert sorted(grouped.values()) == [
        ["other, first", "other, second"],
        ["visitor, first", "visitor, second"],
    ]
    conversations = [turn.conversation for turn in turns]
    assert conversations == sorted(conversations), "never interleaved"


async def test_volume_counts_questions_and_the_conversations_they_were_in() -> None:
    await _turn(VISITOR, "one", NOON)
    await _turn(VISITOR, "two", NOON + dt.timedelta(minutes=1))
    await _turn(OTHER, "three", NOON + dt.timedelta(minutes=2))
    await _turn(OTHER, "outside", NOON + 2 * HOUR)

    volume = await analytics_db.volume_between(NOON, NOON + HOUR)
    nothing = await analytics_db.volume_between(NOON - 2 * HOUR, NOON - HOUR)

    assert (volume.questions, volume.conversations) == (3, 2), "answers are not questions"
    assert (nothing.questions, nothing.conversations) == (0, 0)


# --- which articles the answers drew on -------------------------------------------
#
# corpus.seed names each section's slug "section-0", "section-1", ... in order, and
# its heading whatever the test passes: "a0", "a1", ...


async def _chunk_ids(doc_id: str) -> list[int]:
    rows = await _all(
        "select c.id from chunks c join documents d on d.id = c.document_id "
        "where d.doc_id = %s order by c.chunk_index",
        (doc_id,),
    )
    return [int(row["id"]) for row in rows]  # type: ignore[call-overload]


async def _knowledge_base() -> None:
    await corpus.seed("about", "About", [(f"a{i}", "text", corpus.axis(i)) for i in range(3)])
    await corpus.seed(
        "services", "Services", [(f"s{i}", "text", corpus.axis(3 + i)) for i in (0, 1)]
    )
    await corpus.seed("contact", "Contact", [("c0", "text", corpus.axis(5))])


def _usage_rows(usage: analytics_db.ArticleUsage) -> list[tuple[str, int, list[str], int]]:
    return [(item.doc_id, item.sections, item.unused, item.answers) for item in usage.articles]


async def test_an_article_is_counted_for_an_answer_whose_best_passages_include_it() -> None:
    await _knowledge_base()

    # Best two: about's first section and services' first. Three more came back
    # lower down, and a second search returned another: none of that counts.
    await _turn(
        VISITOR,
        "one",
        NOON,
        _result(
            searches=[
                _searched(
                    ("about", "section-0"),
                    ("services", "section-0"),
                    ("about", "section-1"),
                    ("about", "section-2"),
                    ("services", "section-1"),
                ),
                _searched(("services", "section-1")),
            ]
        ),
    )
    # Best two: two sections of one article. One answer, two of its sections.
    await _turn(
        VISITOR,
        "two",
        NOON + dt.timedelta(minutes=1),
        _result(searches=[_searched(("about", "section-0"), ("about", "section-1"))]),
    )
    # Small talk searches nothing, and is not an answer that "drew on" anything.
    await _turn(VISITOR, "hello", NOON + dt.timedelta(minutes=2), _result(route="small_talk"))
    # Outside the window.
    await _turn(
        VISITOR,
        "later",
        NOON + 2 * HOUR,
        _result(searches=[_searched(("services", "section-1"), ("contact", "section-0"))]),
    )

    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=2)

    assert (usage.searched, usage.counted) == (2, 2)
    assert _usage_rows(usage) == [
        ("about", 3, ["a2"], 2),
        ("services", 2, ["s1"], 1),
        # Listed although nothing drew on it: the unused articles are the point.
        ("contact", 1, ["c0"], 0),
    ]
    assert [item.sections_used for item in usage.articles] == [2, 1, 0]


async def test_an_article_keeps_its_answers_when_it_is_indexed_again() -> None:
    """The defect this replaced. Counted by chunk id, an answer lost its credit to an
    article the moment that article was edited: indexing it again deletes its chunks
    and inserts new ones, with new ids. The report then showed the article that had
    just been rewritten as barely used, beside a note saying nothing was lost."""
    await _knowledge_base()
    before = await _chunk_ids("about")
    await _turn(
        VISITOR,
        "before the edit",
        NOON,
        _result(searches=[_searched(("about", "section-0"), ("services", "section-0"))]),
    )

    await corpus.seed("about", "About", [(f"a{i}", "text", corpus.axis(i)) for i in range(3)])

    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=2)

    assert set(before).isdisjoint(await _chunk_ids("about")), "indexed again: every id is new"
    assert (usage.searched, usage.counted) == (1, 1)
    assert _usage_rows(usage) == [
        ("about", 3, ["a1", "a2"], 1),
        ("services", 2, ["s1"], 1),
        ("contact", 1, ["c0"], 0),
    ]


async def test_an_answer_stored_before_articles_were_recorded_is_left_out_of_the_counts() -> None:
    """Such a row has chunk ids and nothing else. It searched; it cannot be credited
    to any article, and it is not guessed at from ids that may point elsewhere now."""
    await _knowledge_base()
    answer_id = await _turn(
        VISITOR, "an old answer", NOON, _result(searches=[_searched(("about", "section-0"))])
    )
    # As a row written by the earlier code is: the same record without the two keys.
    await _execute(
        "update chat_messages "
        "set tool_calls = jsonb_build_array((tool_calls -> 0) - 'doc_ids' - 'sections') "
        "where id = %s",
        (answer_id,),
    )

    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=5)

    assert (usage.searched, usage.counted) == (1, 0)
    assert [item.answers for item in usage.articles] == [0, 0, 0]


async def test_a_search_that_found_nothing_is_counted_and_credits_nothing() -> None:
    await _knowledge_base()
    await _turn(VISITOR, "nothing came back", NOON, _result(searches=[_searched()]))

    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=5)

    assert (usage.searched, usage.counted) == (1, 1)
    assert [item.answers for item in usage.articles] == [0, 0, 0]


async def test_a_section_with_no_slug_is_matched_by_its_absence() -> None:
    """`=` between two nulls is null, which a `where` reads as no. The match is
    written `is not distinct from` so that a section without a slug, recorded as
    null, is still the section it is."""
    await _knowledge_base()
    await _execute(
        "update chunks set section = null "
        "where document_id = (select id from documents where doc_id = 'contact')"
    )
    await _turn(
        VISITOR, "how do I reach him", NOON, _result(searches=[_searched(("contact", None))])
    )

    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=5)

    contact = next(item for item in usage.articles if item.doc_id == "contact")
    assert (contact.answers, contact.unused) == (1, [])


async def test_no_answers_and_no_articles_is_zeros_and_an_empty_list() -> None:
    usage = await analytics_db.article_usage(NOON, NOON + HOUR, best=5)

    assert (usage.searched, usage.counted, usage.articles) == (0, 0, [])


# --- conversations by prefix (analytics seed) -------------------------------------


async def test_a_prefix_matches_what_starts_with_it_and_nothing_else() -> None:
    """`like 'invented_%'` would match inventedXthree as well: in a like pattern an
    underscore is "any one character". starts_with takes the prefix as written."""
    for session_id in ("invented_one", "invented_two", "inventedXthree", VISITOR):
        await _turn(session_id, "A question?", NOON)

    assert await analytics_db.count_conversations("invented_") == 2
    assert await analytics_db.delete_conversations("invented_") == 2

    remaining = await _all("select session_id from chat_sessions order by session_id")
    assert [row["session_id"] for row in remaining] == [VISITOR, "inventedXthree"]
    assert len(await _all("select id from chat_messages")) == 4, "their turns went with them"


async def test_an_empty_prefix_is_refused_rather_than_matching_everything() -> None:
    await _turn(VISITOR, "A question?", NOON)

    with pytest.raises(ValueError, match="cannot be empty"):
        await analytics_db.count_conversations("")
    with pytest.raises(ValueError, match="cannot be empty"):
        await analytics_db.delete_conversations("")

    assert len(await _all("select id from chat_sessions")) == 1


async def test_backdating_moves_one_conversation_and_keeps_its_shape() -> None:
    await _turn("invented-one", "first", NOON)
    rated = await _turn("invented-one", "second", NOON + dt.timedelta(seconds=30))
    other = await _turn(VISITOR, "untouched", NOON)
    for session_id, answer_id in (("invented-one", rated), (VISITOR, other)):
        await feedback_db.record_feedback(
            session_id=session_id, message_id=answer_id, rating=1, comment=None
        )
    votes_before = await _all("select message_id, created_at from message_feedback")
    before = await _all(
        "select session_id, created_at, last_seen_at from chat_sessions order by session_id"
    )

    await analytics_db.backdate_conversation("invented-one", dt.timedelta(days=3))

    moved = await analytics_db.turns_between(
        NOON - dt.timedelta(days=4), NOON - dt.timedelta(days=2)
    )
    assert [(turn.question, turn.asked_at) for turn in moved] == [
        ("first", NOON - dt.timedelta(days=3)),
        ("second", NOON - dt.timedelta(days=3) + dt.timedelta(seconds=30)),
    ]
    stayed = await analytics_db.turns_between(NOON - HOUR, NOON + HOUR)
    assert [turn.question for turn in stayed] == ["untouched"]

    after = await _all(
        "select session_id, created_at, last_seen_at from chat_sessions order by session_id"
    )
    by_id = {row["session_id"]: row for row in before}
    for row in after:
        was = by_id[row["session_id"]]
        shift = dt.timedelta(days=3) if row["session_id"] == "invented-one" else dt.timedelta(0)
        assert was["created_at"] - row["created_at"] == shift  # type: ignore[operator]
        assert was["last_seen_at"] - row["last_seen_at"] == shift  # type: ignore[operator]

    # A thumbs up left on a moved answer moves with it. Otherwise the vote would be
    # days newer than the answer it was left on.
    was_voted = {row["message_id"]: row["created_at"] for row in votes_before}
    for row in await _all("select message_id, created_at from message_feedback"):
        shift = dt.timedelta(days=3) if row["message_id"] == rated else dt.timedelta(0)
        assert was_voted[row["message_id"]] - row["created_at"] == shift  # type: ignore[operator]
