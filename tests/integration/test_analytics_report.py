"""Building a report, end to end: the queries, the embedding of the questions, the signals.

Real database; the embeddings are the fake's, which lets a test decide how alike two
questions are. A question embedded along an axis and a passage stored along the same
axis score 1; two different axes score 0.
"""

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal

import corpus
import pytest
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.analytics import digest as digests
from portfolio_ai.analytics import report as reports
from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.pool import get_pool

pytestmark = pytest.mark.integration

VISITOR = "aaaaaaaa-1111-4222-8333-444444444444"
OTHER = "bbbbbbbb-1111-4222-8333-444444444444"
THIRD = "cccccccc-1111-4222-8333-444444444444"

UNTIL = dt.datetime(2026, 10, 13, 9, 0, tzinfo=dt.UTC)
SINCE = UNTIL - dt.timedelta(days=7)
MONDAY = UNTIL - dt.timedelta(days=1)

RATE = "What is his day rate?"
RATE_AGAIN = "How much does he charge per day?"
LARAVEL = "Does Mihail work with Laravel?"
WEATHER = "Where is Mihail in this weather?"
SUM = "What is 17 times 23?"
DISTINCT = sorted([RATE, RATE_AGAIN, LARAVEL, WEATHER, SUM])


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    fake = install_fake_openai(monkeypatch)
    fake.vectors = {
        # The same direction: one question asked twice.
        RATE: corpus.axis(10),
        RATE_AGAIN: corpus.axis(10),
        # Unrelated to either.
        LARAVEL: corpus.axis(11),
        # Close to the "Where is Mihail based?" passage, which sits on axis 0.
        WEATHER: corpus.between(0, 12, 0.6),
        # Close to nothing indexed: a tenth of the way towards the other passage,
        # so that which passage is "nearest" is not a tie.
        SUM: corpus.between(13, 1, 0.1),
    }
    return fake


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., None]]:
    """Set settings through the environment, as `.env` or a container would.

    The settings are cached for the life of the process, so the cache is cleared
    when a value is set and again when the test ends: the next test must not build
    its report with this one's thresholds.
    """
    names: list[str] = []

    def apply(**values: str) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name.upper(), value)
            names.append(name.upper())
        get_settings.cache_clear()

    yield apply

    for name in names:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def knowledge_base() -> None:
    await corpus.clear()
    await corpus.seed(
        "about-mihail",
        "About Mihail",
        [
            ("Where is Mihail based?", "Sofia, Bulgaria.", corpus.axis(0)),
            ("What does Mihail do?", "He is a Solutions Architect.", corpus.axis(1)),
        ],
    )


def _result(route: str = "mihail_related", *, score: float | None = 0.6) -> TurnResult:
    return TurnResult(
        reply="An answer.",
        classification=route,  # type: ignore[arg-type]
        citations=[],
        searches=[],
        retrieved_chunk_ids=[],
        top_score=score if route == "mihail_related" else None,
        fallback_used=False,
        links_removed=0,
        calls=[],
        model="a-model",
        latency_ms=4000,
        first_token_ms=3000,
    )


async def _turn(session_id: str, question: str, asked_at: dt.datetime, result: TurnResult) -> None:
    session_pk = await chat_db.ensure_session(session_id)
    await chat_db.insert_turn(session_pk, question, result, asked_at)


async def _conversations() -> None:
    minute = dt.timedelta(minutes=1)
    await _turn(VISITOR, "Hello", MONDAY, _result("small_talk"))
    await _turn(VISITOR, RATE, MONDAY + minute, _result(score=0.31))
    await _turn(VISITOR, RATE_AGAIN, MONDAY + 2 * minute, _result(score=0.33))
    await _turn(VISITOR, LARAVEL, MONDAY + 3 * minute, _result())
    await _turn(OTHER, SUM, MONDAY, _result("out_of_scope"))
    await _turn(OTHER, WEATHER, MONDAY + minute, _result("out_of_scope"))
    # Somebody else asking the first visitor's question, word for word.
    await _turn(THIRD, RATE, MONDAY, _result(score=0.31))
    # The week before: counted, and nothing else.
    await _turn(OTHER, "An older question?", SINCE - dt.timedelta(days=2), _result())


async def test_a_report_is_built_from_the_window_with_each_question_embedded_once(
    fake: FakeOpenAI,
) -> None:
    await _conversations()

    report = await reports.build(since=SINCE, until=UNTIL)

    assert len(report.turns) == 7
    assert report.conversations == 3
    assert (report.previous.questions, report.previous.conversations) == (1, 1)

    # --- one request; a question two visitors asked embedded once; small talk never
    assert fake.embedding_batches == [5]
    assert sorted(fake.embedded) == DISTINCT
    assert fake.requests == [], "no model was asked anything"

    # --- and what that cost, carried on the report, since nothing else records it
    assert (report.embedded, report.embedding_tokens) == (5, 25)
    assert report.embedding_cost > 0

    # --- every question beside the one before it
    assert [
        (pair.first.question, pair.second.question, round(pair.similarity, 3))
        for pair in report.pairs
    ] == [(RATE, RATE_AGAIN, 1.0), (RATE_AGAIN, LARAVEL, 0.0), (SUM, WEATHER, 0.0)]

    # --- refused questions, the one nearest a passage first
    assert [
        (item.turn.question, round(item.score or 0.0, 2), item.title, item.section)
        for item in report.refused
    ] == [
        (WEATHER, 0.8, "About Mihail", "Where is Mihail based?"),
        (SUM, 0.1, "About Mihail", "What does Mihail do?"),
    ]


async def test_the_questions_are_embedded_a_batch_at_a_time(
    fake: FakeOpenAI, configured: Callable[..., None]
) -> None:
    """Not "one call": one for every EMBEDDING_BATCH_SIZE questions. 64 by default, so
    a quiet week is one request and a busy quarter is several."""
    configured(embedding_batch_size="2")
    await _conversations()

    report = await reports.build(since=SINCE, until=UNTIL)

    assert fake.embedding_batches == [2, 2, 1]
    assert sorted(fake.embedded) == DISTINCT
    assert report.embedded == 5


@pytest.mark.parametrize(
    ("thresholds", "thin", "asked_again"),
    [
        (("0.32", "0.9"), [RATE, RATE], 1),
        (("0.34", "0.0"), [RATE, RATE, RATE_AGAIN], 3),
    ],
    ids=["strict", "loose"],
)
async def test_the_two_thresholds_come_from_the_settings_and_draw_the_lists(
    fake: FakeOpenAI,
    configured: Callable[..., None],
    thresholds: tuple[str, str],
    thin: list[str],
    asked_again: int,
) -> None:
    """Run twice, with two settings, on the same stored turns. A `build` that ignored
    LOW_SCORE_THRESHOLD or REPHRASE_SIMILARITY, or swapped them, fails one run."""
    low, alike = thresholds
    configured(low_score_threshold=low, rephrase_similarity=alike)
    await _conversations()

    report = await reports.build(since=SINCE, until=UNTIL)
    assert fake.pending == 0

    assert report.low_score_threshold == pytest.approx(float(low))
    assert report.rephrase_similarity == pytest.approx(float(alike))
    assert [turn.question for turn in report.thin] == thin
    assert len(report.asked_again) == asked_again

    text = reports.render(report, verbatim=True)
    assert f"(best passage under {float(low):.2f})" in text
    assert f"0.310  {RATE}" in text
    assert '    nearest: About Mihail, "Where is Mihail based?"' in text

    emailed = reports.render(report, verbatim=False)
    for question in (RATE, RATE_AGAIN, LARAVEL, WEATHER, SUM, "Hello"):
        assert question not in emailed


@pytest.mark.usefixtures("fake")
async def test_the_weeks_email_is_composed_from_what_is_stored_and_quotes_none_of_it() -> None:
    """The whole path the Monday job takes, short of the mail server: stored turns,
    a report built from them, an email composed from the report."""
    await _conversations()

    message = digests.compose(await reports.build(since=SINCE, until=UNTIL))

    assert message.subject == (
        "mihaylov.io chat: 7 questions in the 7 days to 2026-10-13, 0 not answered"
    )
    assert "- 7 questions in 3 conversations (the 7 days before: 1 in 1)" in message.body
    assert "- 4 about Mihail, 1 small talk, 2 off-topic" in message.body
    for question in (RATE, RATE_AGAIN, LARAVEL, WEATHER, SUM, "Hello", "An older question?"):
        assert question not in message.body
        assert question not in message.subject


async def test_a_window_nobody_asked_in_builds_and_embeds_nothing(fake: FakeOpenAI) -> None:
    report = await reports.build(since=SINCE, until=UNTIL)

    assert (report.turns, report.pairs, report.refused) == ([], [], [])
    assert (fake.embedded, fake.embedding_batches) == ([], [])
    assert (report.embedded, report.embedding_tokens) == (0, 0)
    assert report.embedding_cost == Decimal(0)
    assert "Nobody asked anything" in reports.render(report, verbatim=True)


@pytest.mark.usefixtures("fake")
async def test_a_refused_question_with_nothing_indexed_has_no_nearest_passage() -> None:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("delete from documents")
    await _turn(OTHER, SUM, MONDAY, _result("out_of_scope"))

    report = await reports.build(since=SINCE, until=UNTIL)

    [refused] = report.refused
    assert (refused.score, refused.title, refused.section) == (None, None, None)
    assert "    nearest: nothing indexed" in reports.render(report, verbatim=True)
