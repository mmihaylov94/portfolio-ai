"""Content gaps, from stored answers to rows in ``content_gaps``.

Real database, real migrations, the real clustering and the real SQL. Only OpenAI is
a fake: the test decides which way each search's embedding points, and scripts the
label the model gives each new gap.

Three subjects appear throughout. Two searches about rates point the same way, two
about references another, and one about hobbies a third, so the groups are known
before anything runs: rates and references with two questions each, hobbies alone.
"""

import datetime as dt

import corpus
import pytest
from log_recorder import LogRecorder
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.analytics import gaps
from portfolio_ai.assistant.agent import Search, TurnResult
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db import feedback as feedback_db
from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import AssistantError
from portfolio_ai.llm import embeddings, responses

pytestmark = pytest.mark.integration

VISITOR = "aaaaaaaa-1111-4222-8333-444444444444"
NOON = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)
MINUTE = dt.timedelta(minutes=1)

DAY_RATE = "consulting day rate pricing"
PRICE_LIST = "services price list rates"
CLIENTS = "client references testimonials"
EMPLOYERS = "employer references recommendations"
HOBBIES = "hobbies interests free time"
SALARY = "salary expectations compensation"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    fake = install_fake_openai(monkeypatch)
    fake.vectors = {
        # Two searches a little apart on one axis: one subject.
        DAY_RATE: corpus.axis(20),
        PRICE_LIST: corpus.between(20, 21, 0.3),
        CLIENTS: corpus.axis(30),
        EMPLOYERS: corpus.between(30, 31, 0.3),
        HOBBIES: corpus.axis(40),
        SALARY: corpus.axis(50),
    }
    return fake


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from content_gaps")


async def _ask(  # ruff: ignore[too-many-arguments] -- one turn's worth, all named
    question: str,
    query: str,
    *,
    minutes: float = 0,
    fallback: bool = True,
    score: float | None = 0.30,
    route: str = "mihail_related",
    rating: int | None = None,
) -> int:
    """Store one answered question, asked ``minutes`` after noon. Returns its id."""
    result = TurnResult(
        reply="An answer.",
        classification=route,  # type: ignore[arg-type]
        citations=[],
        searches=[
            Search(
                query=query,
                chunk_ids=[],
                hits=0,
                top_score=score,
                duration_ms=30,
                doc_ids=[],
                sections=[],
            )
        ]
        if route == "mihail_related"
        else [],
        retrieved_chunk_ids=[],
        top_score=score if route == "mihail_related" else None,
        fallback_used=fallback,
        links_removed=0,
        calls=[],
        model="a-model",
        latency_ms=4000,
        first_token_ms=3000,
    )
    session_pk = await chat_db.ensure_session(VISITOR)
    answer_id = await chat_db.insert_turn(session_pk, question, result, NOON + minutes * MINUTE)
    if rating is not None:
        await feedback_db.record_feedback(
            session_id=VISITOR, message_id=answer_id, rating=rating, comment=None
        )
    return answer_id - 1


async def _three_subjects() -> dict[str, int]:
    """Five failed questions on three subjects. Returns each question's id by its search."""
    return {
        DAY_RATE: await _ask("What's his day rate?", DAY_RATE, minutes=0),
        CLIENTS: await _ask("Can I talk to a client?", CLIENTS, minutes=1, fallback=False),
        PRICE_LIST: await _ask("Is there a price list?", PRICE_LIST, minutes=2, rating=-1),
        HOBBIES: await _ask("Any hobbies?", HOBBIES, minutes=3, score=0.21),
        EMPLOYERS: await _ask("References from employers?", EMPLOYERS, minutes=4),
    }


async def _purge(*question_ids: int) -> None:
    """Delete questions, as the nightly purge does in the end. Their answers go with
    them, through the foreign key from an answer to the question it replies to."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_messages where id = any(%s)", (list(question_ids),))


async def _rows() -> list[dict[str, object]]:
    """Every gap as stored, oldest first. The centre is read as text, so that two
    readings of an unchanged row compare equal whatever type a vector comes back as."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select id, cluster_label, centroid::text as centre, question_count, fallback_count,
                   avg_top_score, negative_rate, example_message_ids, status, resolved_doc_id,
                   resolved_at, first_seen_at, last_seen_at, refreshed_at
            from content_gaps
            order by id
            """
        )
        return [dict(row) for row in await cur.fetchall()]


# --- which questions failed -------------------------------------------------------


async def test_a_question_failed_when_its_answer_declined_or_found_nothing_close() -> None:
    declined = await _ask("What's his day rate?", DAY_RATE, fallback=True, score=0.50)
    thin = await _ask("Any hobbies?", HOBBIES, minutes=1, fallback=False, score=0.34)
    await _ask("Does he use Laravel?", "Laravel PHP", minutes=2, fallback=False, score=0.35)
    await _ask("Hello", "", minutes=3, route="small_talk", fallback=False)
    await _ask("What is 2 + 2?", "", minutes=4, route="out_of_scope", fallback=False)

    failed = await analytics_db.failed_questions(low_score=0.35, limit=100)

    assert [(item.question_id, item.question, item.query) for item in failed] == [
        (declined, "What's his day rate?", DAY_RATE),
        (thin, "Any hobbies?", HOBBIES),
    ], "on the threshold is not under it, and only the knowledge-base route can fail"
    assert [item.fallback_used for item in failed] == [True, False]
    # Exactly, not approximately: the column is a `real`, and 0.34 comes back out of
    # it as 0.3400000035762787 unless it is rounded on the way.
    assert [item.top_score for item in failed] == [0.5, 0.34]
    assert failed[0].asked_at == NOON


async def test_a_failed_question_carries_its_thumbs_down_if_it_got_one() -> None:
    await _ask("What's his day rate?", DAY_RATE, rating=-1)
    await _ask("Any hobbies?", HOBBIES, minutes=1)

    failed = await analytics_db.failed_questions(low_score=0.35, limit=100)

    assert [item.rating for item in failed] == [-1, None]


async def test_a_limit_keeps_the_newest_failed_questions_in_the_order_they_were_asked() -> None:
    ids = await _three_subjects()

    newest = await analytics_db.failed_questions(low_score=0.35, limit=3)

    assert [item.question_id for item in newest] == [ids[PRICE_LIST], ids[HOBBIES], ids[EMPLOYERS]]


# --- the first refresh ------------------------------------------------------------


async def test_the_first_refresh_groups_names_and_stores_each_gap(fake: FakeOpenAI) -> None:
    ids = await _three_subjects()
    # Named largest group first; groups of equal size in the order they were formed.
    fake.label("Consulting day rates")
    fake.label("client references")
    fake.label("Hobbies and interests")

    result = await gaps.refresh()

    assert fake.pending == 0, "one naming call for each new gap, and no more"
    assert (result.failed, len(result.created), len(result.updated), result.emptied) == (5, 3, 0, 0)
    assert result.embedded == 5
    assert result.cost > 0
    assert (result.capped, result.unnamed) == (False, 0)

    rates, references, hobbies = await _rows()
    assert [row["cluster_label"] for row in (rates, references, hobbies)] == [
        "Consulting day rates",
        "Client references",
        "Hobbies and interests",
    ]
    assert [row["question_count"] for row in (rates, references, hobbies)] == [2, 2, 1]
    assert [row["fallback_count"] for row in (rates, references, hobbies)] == [2, 1, 1]
    assert [row["status"] for row in (rates, references, hobbies)] == ["open"] * 3

    assert rates["negative_rate"] == pytest.approx(0.5), "one thumbs down in two"
    assert hobbies["avg_top_score"] == pytest.approx(0.21)
    assert (rates["first_seen_at"], rates["last_seen_at"]) == (NOON, NOON + 2 * MINUTE)
    assert rates["refreshed_at"] is not None
    assert rates["resolved_at"] is None

    # The examples are question ids, and never the questions.
    assert sorted(rates["example_message_ids"]) == sorted([ids[DAY_RATE], ids[PRICE_LIST]])  # type: ignore[call-overload]
    assert hobbies["example_message_ids"] == [ids[HOBBIES]]


async def test_what_the_model_is_asked_to_name_is_the_search_and_the_question(
    fake: FakeOpenAI,
) -> None:
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")

    await gaps.refresh()

    [request] = fake.requests
    assert request["input"] == [
        {"role": "user", "content": f'1. searched "{DAY_RATE}", asked "What\'s his day rate?"'}
    ]
    assert fake.embedded == [DAY_RATE], "it is the search that is embedded, not the question"


async def test_a_search_several_answers_ran_is_embedded_once(fake: FakeOpenAI) -> None:
    await _ask("What's his day rate?", DAY_RATE)
    await _ask("And his day rate?", DAY_RATE, minutes=1)
    fake.label("Consulting day rates")

    result = await gaps.refresh()

    assert fake.embedded == [DAY_RATE]
    assert result.embedded == 1
    [row] = await _rows()
    assert row["question_count"] == 2, "counted twice, though: two people asked"


# --- later refreshes --------------------------------------------------------------


async def test_a_second_refresh_recognises_every_gap_and_names_nothing(fake: FakeOpenAI) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    before = await _rows()

    result = await gaps.refresh()

    assert fake.pending == 0
    assert len(fake.requests) == 3, "no model was asked anything the second time"
    assert (len(result.created), len(result.updated), result.emptied) == (0, 3, 0)
    after = await _rows()
    assert [row["id"] for row in after] == [row["id"] for row in before], "no duplicates"
    assert [row["cluster_label"] for row in after] == [row["cluster_label"] for row in before]


async def test_a_new_question_on_a_known_subject_joins_its_gap_and_keeps_what_was_decided(
    fake: FakeOpenAI,
) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    rates_id = (await _rows())[0]["id"]
    assert await analytics_db.set_gap_status(rates_id, "wont_fix", doc_id=None)  # type: ignore[arg-type]

    # A week on, somebody asks about rates again, and somebody about a new subject.
    late = await _ask("What does a day cost?", DAY_RATE, minutes=7 * 24 * 60)
    await _ask("What salary does he want?", SALARY, minutes=7 * 24 * 60 + 1)
    fake.label("Salary expectations")

    result = await gaps.refresh()

    assert fake.pending == 0, "one naming call, for the new subject only"
    assert (len(result.created), len(result.updated)) == (1, 3)
    rows = {row["cluster_label"]: row for row in await _rows()}
    assert set(rows) == {
        "Consulting day rates",
        "Client references",
        "Hobbies and interests",
        "Salary expectations",
    }
    rates = rows["Consulting day rates"]
    assert rates["id"] == rates_id
    assert rates["question_count"] == 3
    assert rates["status"] == "wont_fix", "a decision outlives a refresh"
    assert rates["first_seen_at"] == NOON
    assert rates["last_seen_at"] == NOON + dt.timedelta(days=7)
    assert late in rates["example_message_ids"]  # type: ignore[operator]


async def test_a_gap_nobody_asks_about_any_more_is_emptied_and_kept(fake: FakeOpenAI) -> None:
    """What the purge does to a gap in the end: its questions go, and the next refresh
    finds none. The row stays, with its label, its status and its dates, so that the
    subject is recognised when it comes round again."""
    ids = await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    hobbies_id = (await _rows())[2]["id"]
    assert await analytics_db.set_gap_status(hobbies_id, "wont_fix", doc_id=None)  # type: ignore[arg-type]

    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_messages where id = %s", (ids[HOBBIES],))

    result = await gaps.refresh()

    assert (len(result.updated), result.emptied) == (2, 1)
    hobbies = (await _rows())[2]
    assert hobbies["question_count"] == 0
    assert hobbies["example_message_ids"] == []
    assert hobbies["avg_top_score"] is None
    assert (hobbies["cluster_label"], hobbies["status"]) == ("Hobbies and interests", "wont_fix")
    assert hobbies["first_seen_at"] == NOON + 3 * MINUTE, "its history is kept"

    # And when somebody asks again, it is the same gap, still decided against.
    await _ask("Does he have hobbies?", HOBBIES, minutes=9000)
    again = await gaps.refresh()

    assert (len(again.created), len(again.updated), again.emptied) == (0, 3, 0)
    hobbies = (await _rows())[2]
    assert (hobbies["id"], hobbies["question_count"], hobbies["status"]) == (
        hobbies_id,
        1,
        "wont_fix",
    )
    assert len(fake.requests) == 3, "recognised, so not named again"


async def test_a_gap_emptied_once_is_not_emptied_again(fake: FakeOpenAI) -> None:
    ids = await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_messages where id = %s", (ids[HOBBIES],))

    assert (await gaps.refresh()).emptied == 1
    assert (await gaps.refresh()).emptied == 0


async def test_with_no_failed_question_left_every_gap_is_emptied(fake: FakeOpenAI) -> None:
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_sessions")

    result = await gaps.refresh()

    assert (result.failed, result.groups, result.emptied) == (0, [], 1)
    assert fake.embedded == [DAY_RATE], "nothing to embed the second time"
    [row] = await _rows()
    assert row["question_count"] == 0


# --- what a refresh must not do ---------------------------------------------------


async def test_a_dry_run_names_nothing_and_writes_nothing(fake: FakeOpenAI) -> None:
    await _three_subjects()

    result = await gaps.refresh(dry_run=True)

    assert fake.requests == [], "no model was asked for a name"
    assert await _rows() == []
    assert result.dry_run
    assert [(len(group.questions), group.gap_id, group.label) for group in result.groups] == [
        (2, None, None),
        (2, None, None),
        (1, None, None),
    ]


async def test_a_dry_run_says_which_stored_gap_each_group_is(fake: FakeOpenAI) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    await _ask("What salary does he want?", SALARY, minutes=10)

    result = await gaps.refresh(dry_run=True)

    assert [(group.label, group.gap_id is not None) for group in result.groups] == [
        ("Consulting day rates", True),
        ("Client references", True),
        ("Hobbies and interests", True),
        (None, False),
    ]
    assert len(await _rows()) == 3, "and it wrote nothing"


async def test_a_refresh_that_dies_while_naming_leaves_the_table_as_it_was(
    fake: FakeOpenAI,
) -> None:
    """Every name is asked for before anything is written. Two gaps are new here. A
    refresh that gets one name and fails on the next has changed nothing: not the
    gap it had a name for, and not the known gap's count."""
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    before = await _rows()

    await _ask("Can I talk to a client?", CLIENTS, minutes=1)
    await _ask("Any hobbies?", HOBBIES, minutes=2)
    await _ask("Another day rate question?", DAY_RATE, minutes=3)
    fake.label("Client references")
    fake.http_error(500)

    with pytest.raises(AssistantError):
        await gaps.refresh()

    assert await _rows() == before, "the known gap's count was not updated either"


async def test_a_dry_run_counts_the_gaps_the_refresh_it_describes_would_empty(
    fake: FakeOpenAI,
) -> None:
    """It printed 0 whatever was true, and the refresh that followed emptied one."""
    ids = await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    await _purge(ids[HOBBIES])

    plan = await gaps.refresh(dry_run=True)

    assert plan.emptied == 1
    assert [row["question_count"] for row in await _rows()] == [2, 2, 1], "and emptied nothing"
    assert (await gaps.refresh()).emptied == 1, "which is what the refresh then did"


async def test_nothing_a_refresh_logs_holds_a_question_a_search_or_a_label(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`digest` runs this on a Monday, where a log line is the container's log. The
    commands' tests check what they print, over a stand-in for the refresh; this is
    the refresh itself, with every module it logs from recorded.

    One label is refused on the way, so that the line written for that is here too.
    """
    recorder = LogRecorder()
    for module in (gaps, responses, embeddings):
        monkeypatch.setattr(module, "log", recorder)
    await _three_subjects()
    labels = ("Consulting day rates", "Client references at https://x.example", "Hobbies")
    for label in labels:
        fake.label(label)

    await gaps.refresh()

    # First, that the lines were recorded at all: a test of what is absent passes
    # on an empty list.
    events = [line["event"] for line in recorder.lines]
    for expected in ("embeddings_created", "llm_call", "gap_label_unusable", "gaps_refreshed"):
        assert expected in events
    questions = (
        "What's his day rate?",
        "Can I talk to a client?",
        "Is there a price list?",
        "Any hobbies?",
        "References from employers?",
    )
    for private in (*questions, DAY_RATE, PRICE_LIST, CLIENTS, EMPLOYERS, HOBBIES, *labels):
        assert not recorder.holds(private), private


# --- the limits of one refresh ------------------------------------------------------


async def test_only_the_newest_failed_questions_are_grouped_and_the_refresh_says_so(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grouping takes time that grows with the square of the questions, on a server
    that is also serving the site. Past the limit the oldest are left out, and the
    result says that it happened."""
    monkeypatch.setattr(gaps, "MAX_QUESTIONS", 2)
    await _three_subjects()
    fake.label("Hobbies and interests")
    fake.label("Employer references")

    result = await gaps.refresh()

    assert (result.failed, result.capped) == (2, True)
    assert fake.embedded == sorted([HOBBIES, EMPLOYERS]), "the two asked last"
    assert [row["cluster_label"] for row in await _rows()] == [
        "Hobbies and interests",
        "Employer references",
    ]


async def test_exactly_as_many_failed_questions_as_the_limit_is_not_capped(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps, "MAX_QUESTIONS", 5)
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)

    result = await gaps.refresh()

    assert (result.failed, result.capped) == (5, False)


async def test_a_gap_left_unnamed_is_named_by_the_next_refresh_and_keeps_what_was_decided(
    fake: FakeOpenAI,
) -> None:
    """A blank answer, a refusal or a label that was not allowed leaves a placeholder.
    It used to be for good: the next refresh recognised the gap and so never asked."""
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("")
    first = await gaps.refresh()
    [row] = await _rows()
    assert (row["cluster_label"], first.unnamed) == (gaps.UNNAMED, 1)
    assert await analytics_db.set_gap_status(row["id"], "wont_fix", doc_id=None)  # type: ignore[arg-type]

    fake.label("Consulting day rates")
    second = await gaps.refresh()

    assert fake.pending == 0, "it was asked again"
    assert (len(second.created), len(second.updated), second.unnamed) == (0, 1, 0)
    [named] = await _rows()
    assert (named["id"], named["cluster_label"], named["status"]) == (
        row["id"],
        "Consulting day rates",
        "wont_fix",
    )

    await gaps.refresh()
    assert len(fake.requests) == 2, "and once it has a name, never again"


async def test_gaps_past_the_naming_limit_are_stored_unnamed_and_named_next_time(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps, "MAX_NAMED", 2)
    await _three_subjects()
    fake.label("Consulting day rates")
    fake.label("Client references")

    first = await gaps.refresh()

    assert (len(first.created), first.unnamed) == (3, 1)
    assert [row["cluster_label"] for row in await _rows()] == [
        "Consulting day rates",
        "Client references",
        gaps.UNNAMED,
    ], "the two most asked were named"

    fake.label("Hobbies and interests")
    second = await gaps.refresh()

    assert (len(second.updated), second.unnamed) == (3, 0)
    assert (await _rows())[2]["cluster_label"] == "Hobbies and interests"


# --- recognising a gap ------------------------------------------------------------

NEARBY = "what a day of his time costs"
LEFT = "day rate for consulting work"
RIGHT = "fixed price for a whole project"


@pytest.mark.parametrize(
    ("alike", "recognised"),
    [(0.76, True), (0.74, False)],
    ids=["just-over", "just-under"],
)
async def test_a_group_is_a_stored_gap_when_its_centre_is_alike_enough_and_not_otherwise(
    fake: FakeOpenAI, alike: float, recognised: bool
) -> None:
    """SAME_GAP through the function that decides, a hundredth either side of 0.75.

    The stored gap's one question is purged, and the next asks about something whose
    search points ``alike`` along the old centre. Over the line it is the same gap,
    and keeps its name. Under it, it is a new one, and the old gap is emptied. With
    the line at 0, a question about hobbies was filed under "Consulting day rates".

    The two figures are written out and not worked out from the constant, so that
    moving the constant fails this test: it is a decision, and should be made here
    as well as there.
    """
    first = await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    await _purge(first)
    # between() leans `weight` from one axis towards another; its likeness to the
    # first axis is sqrt(1 - weight ** 2), so this weight gives exactly `alike`.
    fake.vectors[NEARBY] = corpus.between(20, 21, (1 - alike**2) ** 0.5)
    await _ask("What does a day cost?", NEARBY, minutes=1)
    if not recognised:
        fake.label("Something else")

    result = await gaps.refresh()

    assert fake.pending == 0
    outcome = (len(result.updated), len(result.created), result.emptied)
    assert outcome == ((1, 0, 0) if recognised else (0, 1, 1))
    labels = [(row["cluster_label"], row["question_count"]) for row in await _rows()]
    assert labels == (
        [("Consulting day rates", 1)]
        if recognised
        else [("Consulting day rates", 0), ("Something else", 1)]
    )


async def test_two_groups_of_one_refresh_cannot_both_be_the_same_stored_gap(
    fake: FakeOpenAI,
) -> None:
    """A stored gap, and two new searches 40 degrees either side of its centre: each
    0.77 like it, which is over the line, and 0.17 like each other, so they are two
    groups. Both would match the gap. The first to be looked at is it; the second is
    a new gap. Without that, the second overwrote the first's counts, and one
    question was in no gap at all."""
    first = await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    await _purge(first)
    lean = 0.6428  # the sine of 40 degrees
    fake.vectors[LEFT] = corpus.between(20, 21, lean)
    fake.vectors[RIGHT] = corpus.between(20, 21, -lean)
    await _ask("What is his day rate for consulting?", LEFT, minutes=1)
    await _ask("Would he quote a fixed price?", RIGHT, minutes=2)
    fake.label("Fixed project pricing")

    result = await gaps.refresh()

    assert fake.pending == 0
    assert (len(result.updated), len(result.created), result.emptied) == (1, 1, 0)
    assert [(row["cluster_label"], row["question_count"]) for row in await _rows()] == [
        ("Consulting day rates", 1),
        ("Fixed project pricing", 1),
    ]


async def test_a_gap_keeps_its_first_and_last_dates_after_those_questions_are_gone(
    fake: FakeOpenAI,
) -> None:
    """Three questions an hour apart. The first and the last are purged, and the gap
    is worked out again from the one in the middle. When the subject was first asked
    about, and last, are facts about the gap that outlive the questions."""
    early = await _ask("What's his day rate?", DAY_RATE, minutes=0)
    await _ask("And for a week?", DAY_RATE, minutes=60)
    late = await _ask("And for a month?", DAY_RATE, minutes=120)
    fake.label("Consulting day rates")
    await gaps.refresh()
    await _purge(early, late)

    await gaps.refresh()

    [row] = await _rows()
    assert row["question_count"] == 1
    assert (row["first_seen_at"], row["last_seen_at"]) == (NOON, NOON + 120 * MINUTE)


async def test_the_nearest_gap_is_found_by_its_centre_and_can_be_ruled_out(
    fake: FakeOpenAI,
) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()
    rates, references, _ = await _rows()

    near_rates = corpus.between(20, 30, 0.2)
    first = await analytics_db.nearest_gap(near_rates, excluding=[])
    second = await analytics_db.nearest_gap(near_rates, excluding=[rates["id"]])  # type: ignore[list-item]

    assert first is not None
    assert (first.id, first.label) == (rates["id"], "Consulting day rates")
    assert first.similarity > gaps.SAME_GAP
    assert second is not None
    assert second.id == references["id"], "the next nearest, once the nearest is spoken for"
    assert second.similarity < gaps.SAME_GAP


async def test_with_no_gaps_stored_nothing_is_nearest() -> None:
    assert await analytics_db.nearest_gap(corpus.axis(1), excluding=[]) is None


# --- deciding about a gap ---------------------------------------------------------


async def test_marking_a_gap_stamps_when_and_opening_it_again_clears_the_stamp(
    fake: FakeOpenAI,
) -> None:
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    [gap] = await analytics_db.list_gaps()
    await corpus.seed("services", "Services", [("Rates", "text", corpus.axis(0))])

    assert await analytics_db.document_exists("services")
    assert not await analytics_db.document_exists("no-such-article")

    assert await analytics_db.set_gap_status(gap.id, "written", doc_id="services")
    [written] = await analytics_db.list_gaps(status="written")
    assert (written.status, written.resolved_doc_id) == ("written", "services")
    assert written.resolved_at is not None
    assert await analytics_db.list_gaps(status="open") == []

    assert await analytics_db.set_gap_status(gap.id, "open", doc_id=None)
    [opened] = await analytics_db.list_gaps()
    assert (opened.status, opened.resolved_doc_id, opened.resolved_at) == ("open", None, None)

    assert not await analytics_db.set_gap_status(gap.id + 1000, "wont_fix", doc_id=None)


async def test_a_question_that_fails_after_a_gap_was_marked_written_shows_on_the_gap(
    fake: FakeOpenAI,
) -> None:
    """The reason resolved_at exists. The stamp is the database's clock, so the
    question that fails afterwards is given a time in the future of it."""
    await _ask("What's his day rate?", DAY_RATE)
    fake.label("Consulting day rates")
    await gaps.refresh()
    [gap] = await analytics_db.list_gaps()
    await corpus.seed("services", "Services", [("Rates", "text", corpus.axis(0))])
    await analytics_db.set_gap_status(gap.id, "written", doc_id="services")

    [quiet] = await analytics_db.list_gaps()
    assert not quiet.failed_since_resolved, "nothing has failed since it was marked"

    tomorrow = (dt.datetime.now(dt.UTC) - NOON) / MINUTE + 24 * 60
    await _ask("So what is his day rate?", DAY_RATE, minutes=tomorrow)
    await gaps.refresh()

    [asked_again] = await analytics_db.list_gaps()
    assert asked_again.failed_since_resolved
    assert asked_again.status == "written", "it is flagged, not reopened: that is a person's call"


# --- reading the table ------------------------------------------------------------


async def test_gaps_are_listed_most_asked_first_then_most_recently_asked(
    fake: FakeOpenAI,
) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()

    listed = await analytics_db.list_gaps()

    # Rates and references have two questions each; references was asked about last.
    assert [(gap.label, gap.question_count) for gap in listed] == [
        ("Client references", 2),
        ("Consulting day rates", 2),
        ("Hobbies and interests", 1),
    ]
    assert listed[0].last_seen_at > listed[1].last_seen_at


async def test_an_examples_text_comes_back_only_while_its_message_is_stored() -> None:
    kept = await _ask("What's his day rate?", DAY_RATE)
    purged = await _ask("Any hobbies?", HOBBIES, minutes=1)
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from chat_messages where id = %s", (purged,))

    found = await analytics_db.questions_by_id([kept, purged, kept + 1])

    assert found == {kept: "What's his day rate?"}, "not the purged one, and not an answer"
    assert await analytics_db.questions_by_id([]) == {}


async def test_deleting_the_gaps_deletes_all_of_them(fake: FakeOpenAI) -> None:
    await _three_subjects()
    for label in ("Consulting day rates", "Client references", "Hobbies and interests"):
        fake.label(label)
    await gaps.refresh()

    assert await analytics_db.delete_gaps() == 3
    assert await _rows() == []
    assert await analytics_db.delete_gaps() == 0
