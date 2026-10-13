"""The report as data and as text, with no database behind it.

``build`` is tested against a real Postgres in tests/integration. Everything here
starts from a :class:`Report` made by hand: what its lists select, and what
``render`` does and does not print in each of its two modes.
"""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest
from turns import START, gap, turn

from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.report import Refused, Report
from portfolio_ai.analytics.signals import Pair
from portfolio_ai.db.analytics import ArticleUsage, ArticleUse, Gap, Turn, Volume
from portfolio_ai.exceptions import AnalyticsError

UNTIL = START + dt.timedelta(days=1)
SINCE = UNTIL - dt.timedelta(days=7)


def _usage(counted: int = 6, searched: int = 8) -> ArticleUsage:
    return ArticleUsage(
        searched=searched,
        counted=counted,
        articles=[
            ArticleUse(
                doc_id="services",
                title="Services",
                sections=6,
                unused=["Summary", "What does an engagement produce?"],
                answers=5,
            ),
            ArticleUse(doc_id="contact", title="Contact", sections=1, unused=["main"], answers=0),
        ],
    )


def _report(
    turns: list[Turn],
    *,
    pairs: list[Pair] | None = None,
    refused: list[Refused] | None = None,
    usage: ArticleUsage | None = None,
    gaps: list[Gap] | None = None,
) -> Report:
    return Report(
        since=SINCE,
        until=UNTIL,
        turns=turns,
        previous=Volume(questions=4, conversations=2),
        pairs=pairs or [],
        refused=refused or [],
        usage=usage or _usage(),
        gaps=gaps or [],
        low_score_threshold=0.35,
        rephrase_similarity=0.60,
        embedded=5,
        embedding_tokens=41,
        embedding_cost=Decimal("0.00000082"),
    )


# Every piece of text a visitor could have typed, or been shown, in the report
# below. None of them may appear in the form that is emailed.
RATE = "What does Mihail charge for a day of consulting?"
RATE_AGAIN = "How much is his daily rate?"
HOBBIES = "Does he have any hobbies outside work?"
WEATHER = "Will it rain in Sofia tomorrow?"
LARAVEL = "Has Mihail shipped anything with Laravel?"
DECLINE = "His day rate is not listed in the information I have."
INVENTED = "He enjoys hiking, as far as the passages suggest."
COMMENT = "That is not what I asked."


def _full() -> Report:
    rate = turn(1, RATE, top_score=0.31, fallback_used=True, answer=DECLINE)
    again = turn(
        3,
        RATE_AGAIN,
        minutes=1,
        top_score=0.3312,
        fallback_used=True,
        answer=DECLINE,
        rating=-1,
        comment=COMMENT,
    )
    hobbies = turn(5, HOBBIES, minutes=2, top_score=0.3481, answer=INVENTED)
    laravel = turn(7, LARAVEL, conversation=2, top_score=0.61, rating=1)
    weather = turn(9, WEATHER, conversation=2, minutes=1, route="out_of_scope", cost="0.0001")
    hello = turn(11, "hello there", conversation=3, route="small_talk", cost="0.0002")
    return _report(
        [rate, again, hobbies, laravel, weather, hello],
        pairs=[
            Pair(first=rate, second=again, similarity=0.74),
            Pair(first=again, second=hobbies, similarity=0.21),
        ],
        refused=[
            Refused(turn=weather, score=0.33, title="About Mihail", section="Where is he based?")
        ],
    )


# --- the window -----------------------------------------------------------------


def test_since_is_a_number_of_days_back_from_now() -> None:
    since, until = reports.window("7d", now=UNTIL)

    assert (since, until) == (UNTIL - dt.timedelta(days=7), UNTIL)


def test_since_defaults_to_the_present_moment_in_utc() -> None:
    since, until = reports.window(" 30d ")

    assert until.tzinfo is dt.UTC, "a naive time would be compared with timestamptz columns"
    assert until - since == dt.timedelta(days=30)
    assert abs(dt.datetime.now(dt.UTC) - until) < dt.timedelta(minutes=1)


@pytest.mark.parametrize("value", ["7", "week", "0d", "-3d", "1.5d", "7 d", "d", "99999d", ""])
def test_anything_else_is_refused_with_an_example(value: str) -> None:
    with pytest.raises(AnalyticsError, match="such as 7d or 30d"):
        reports.window(value, now=UNTIL)


# --- what the lists select ------------------------------------------------------


def test_a_fallback_is_counted_once_and_not_again_as_a_thin_answer() -> None:
    """Both rate questions scored under the threshold and both declined. They are
    the assistant working as intended, and belong in one list, not two."""
    report = _full()

    assert [item.question for item in report.fallbacks] == [RATE, RATE_AGAIN]
    assert [item.question for item in report.thin] == [HOBBIES]


def test_thin_answers_are_lowest_score_first_and_under_the_threshold_only() -> None:
    report = _report(
        [
            turn(1, "a", top_score=0.34),
            turn(3, "b", top_score=0.12),
            turn(5, "on the threshold", top_score=0.35),
            turn(7, "small talk has no score", route="small_talk"),
        ]
    )

    assert [item.question for item in report.thin] == ["b", "a"]


def test_asked_again_is_the_pairs_at_or_over_the_threshold() -> None:
    first, second, third = turn(1, "a"), turn(3, "b"), turn(5, "c")
    report = _report(
        [first, second, third],
        pairs=[
            Pair(first=first, second=second, similarity=0.60),
            Pair(first=second, second=third, similarity=0.59),
        ],
    )

    assert [(pair.first.question, pair.second.question) for pair in report.asked_again] == [
        ("a", "b")
    ]


def test_counts_and_cost() -> None:
    report = _full()

    assert report.conversations == 3
    assert len(report.knowledge_base) == 4
    assert report.thumbs_up == 1
    assert [item.question for item in report.thumbs_down] == [RATE_AGAIN]
    assert report.cost == Decimal("0.0027")


def test_an_empty_window_costs_a_decimal_nothing() -> None:
    """sum() of nothing is the int 0 unless told where to start. The property says it
    returns a Decimal, and a caller dividing it by a Decimal count, or comparing it
    with the spend cap, should get one every time and not only when somebody asked."""
    assert _report([]).cost == Decimal(0)
    assert isinstance(_report([]).cost, Decimal)


def test_a_route_with_no_name_is_still_counted_in_the_volume() -> None:
    """Three routes have names. A turn routed any other way would otherwise vanish
    from the line that says where the questions went."""
    text = reports.render(
        _report([turn(1), turn(3, route="a_fourth_route"), turn(5, route="small_talk")]),
        verbatim=False,
    )

    assert "- 1 about Mihail, 1 small talk, 1 routed some other way" in text


# --- the terminal report --------------------------------------------------------


def test_the_terminal_report_quotes_and_numbers_what_went_wrong() -> None:
    text = reports.render(_full(), verbatim=True)

    assert "# Chat report: 2026-10-06 to 2026-10-13 (7 days)" in text
    assert "- 6 questions in 3 conversations (the 7 days before: 4 in 2)" in text
    assert "- 4 about Mihail, 1 small talk, 1 off-topic" in text
    assert "- 2 of 4 knowledge-base answers said the information was not there (50%)" in text
    assert "- 1 answered with nothing close (best passage under 0.35)" in text
    assert "- 1 question asked again in other words (similarity 0.60 or more)" in text
    assert "- 1 thumbs up, 1 thumbs down" in text
    assert "- 1 refused as off-topic" in text

    # Each list, with the id `promote-case` will take.
    assert f"- #1  0.310  {RATE}" in text
    assert f"- #5  0.348  {HOBBIES}" in text
    assert f"    answered: {INVENTED}" in text
    assert "- #1 then #3  0.74" in text
    assert f"    comment: {COMMENT}" in text
    assert f"- #9  0.330  {WEATHER}" in text
    assert '    nearest: About Mihail, "Where is he based?"' in text


def test_a_score_just_under_the_threshold_is_not_printed_as_the_threshold() -> None:
    """0.3481 is under 0.35 and belongs in the list. To two decimals it would print
    as 0.35, under a heading that says "under 0.35"."""
    text = reports.render(_full(), verbatim=True)

    [line] = [line for line in text.splitlines() if HOBBIES in line]
    assert "0.348" in line
    assert "0.35 " not in line


def test_the_terminal_report_ends_with_what_the_thresholds_are_set_from() -> None:
    text = reports.render(_full(), verbatim=True)

    assert "(LOW_SCORE_THRESHOLD is 0.35)" in text
    assert "  0.30 to 0.35     3  (2 said the information was not there)" in text
    assert "  0.60 to 0.65     1" in text
    assert "(REPHRASE_SIMILARITY is 0.60)" in text
    assert "  0.7 to 0.8     1" in text


def test_a_long_or_many_lined_question_is_one_clipped_line() -> None:
    long = "Tell me\nabout Mihail " + "and his work " * 40
    text = reports.render(_report([turn(1, long, fallback_used=True)]), verbatim=True)

    [line] = [line for line in text.splitlines() if line.startswith("- #1")]
    assert line.endswith("..."), "clipped"
    assert "Tell me about Mihail" in line, "the line break became a space"
    assert len(line) < 200


def test_cost_and_speed_are_over_the_answers_that_searched() -> None:
    report = _report(
        [
            turn(1, "a", first_token_ms=2000, latency_ms=3000),
            turn(3, "b", first_token_ms=4000, latency_ms=6000),
            # Instant, and left out: it would pull the median towards nothing.
            turn(5, "hello", route="small_talk", first_token_ms=10, latency_ms=20),
        ]
    )

    text = reports.render(report, verbatim=True)

    assert "- $0.0018 for 3 answers ($0.0006 each)" in text
    assert "median 3.0s, nine in ten within 3.8s" in text
    assert "- Whole answer: median 4.5s, nine in ten within 5.7s" in text


@pytest.mark.parametrize("verbatim", [True, False])
def test_the_report_says_what_building_it_cost(verbatim: bool) -> None:
    """The command runs at a log level that drops the embeddings call's own line, so
    this is the only record of the spend, in either form."""
    text = reports.render(_full(), verbatim=verbatim)

    assert "- This report: 5 questions embedded, 41 tokens, $0.000001" in text


# --- the form that may be emailed -----------------------------------------------


def test_the_emailed_form_has_the_counts_and_none_of_the_words() -> None:
    """The rule the whole design leans on: what visitors typed stays in the
    database. Every question, answer and comment in the report is looked for."""
    report = _full()

    text = reports.render(report, verbatim=False)

    assert "- 2 of 4 knowledge-base answers said the information was not there (50%)" in text
    assert "- 1 thumbs up, 1 thumbs down" in text
    for item in report.turns:
        assert item.question not in text
        assert item.answer not in text
    assert COMMENT not in text
    assert "Where is he based?" not in text, "nor the passage a refused question was near"
    assert "#" not in text.replace("# Chat report", "").replace("## ", "")


def test_the_emailed_form_lists_the_articles_that_were_used_and_stops_there() -> None:
    text = reports.render(_full(), verbatim=False)

    assert "Services (services)" in text
    assert "Contact (contact)" not in text, "an unused article is for the terminal"
    assert "For setting the thresholds" not in text


def test_unused_sections_are_not_named_on_a_handful_of_answers() -> None:
    few = reports.render(_report([turn(1)], usage=_usage(counted=6)), verbatim=True)

    assert "Services (services), 6 sections" in few
    assert "not named until 50 answers are counted (6 so far)" in few
    assert "never among the best" not in few


def test_with_enough_answers_the_sections_nobody_reached_are_named() -> None:
    many = reports.render(_report([turn(1)], usage=_usage(counted=50, searched=50)), verbatim=True)

    assert "Services (services), 4 of 6 sections used" in many
    assert '        never among the best 5: "Summary", "What does an engagement produce?"' in many
    assert "Contact (contact), 0 of 1 section used" in many
    assert "not named until" not in many


def test_the_emailed_form_never_names_sections() -> None:
    emailed = reports.render(
        _report([turn(1)], usage=_usage(counted=50, searched=50)), verbatim=False
    )

    assert "never among the best" not in emailed
    assert "Services (services), 6 sections" in emailed


def test_answers_that_cannot_be_counted_are_said_to_be_missing_and_only_then() -> None:
    """Answers stored before they recorded their passages' articles are left out of
    the counts. The report says how many, and says nothing when there are none."""
    some = reports.render(_report([turn(1)], usage=_usage(counted=6, searched=8)), verbatim=True)
    none = reports.render(_report([turn(1)], usage=_usage(counted=8, searched=8)), verbatim=True)

    assert "6 of 8 answers that searched are counted" in some
    assert "that searched are counted" not in none


# --- nothing asked --------------------------------------------------------------


@pytest.mark.parametrize("verbatim", [True, False])
def test_an_empty_window_says_so_in_one_paragraph(verbatim: bool) -> None:
    text = reports.render(_report([]), verbatim=verbatim)

    assert "Nobody asked anything in this window." in text
    assert "The 7 days before: 4 questions in 2 conversations." in text
    assert "##" not in text


# --- content gaps -----------------------------------------------------------------


@pytest.mark.parametrize("verbatim", [True, False])
def test_the_open_gaps_are_listed_most_asked_first_in_both_forms(verbatim: bool) -> None:
    """In the email too. A label is not something a visitor typed: a model wrote it,
    told to name the topic and nobody who asked."""
    report = _report(
        [turn(1)],
        gaps=[
            gap(7, "Consulting day rates", questions=4, fallbacks=3),
            gap(2, "Salary expectations", questions=3, fallbacks=3, last_seen_days_ago=2),
        ],
    )

    text = reports.render(report, verbatim=verbatim)

    assert "## Content gaps" in text
    assert "most asked first, as of 2026-10-12 09:00 UTC." in text
    first = text.index(
        "-    4  Consulting day rates (gap 7; 3 said the information was not there; "
        "last asked 2026-10-11)"
    )
    second = text.index("-    3  Salary expectations (gap 2; 3 said the information was not there")
    assert first < second
    assert text.index("## Content gaps") < text.index("## Cost and speed")
    # Under a title that says seven days, counts that span every question stored.
    assert "Counted over every question still stored, not over these 7 days alone." in text


@pytest.mark.parametrize("verbatim", [True, False])
def test_a_report_lists_the_ten_most_asked_gaps_and_counts_the_rest(verbatim: bool) -> None:
    """`analytics gaps` lists them all. An email of two hundred lines is one nobody
    reads, and nothing else bounds how many gaps there can be."""
    many = [gap(number, f"Gap number {number}", questions=30 - number) for number in range(1, 13)]

    text = reports.render(_report([turn(1)], gaps=many), verbatim=verbatim)

    listed = [line for line in text.splitlines() if "(gap " in line]
    assert len(listed) == reports.GAPS_LISTED == 10
    assert "Gap number 10 (gap 10;" in listed[-1]
    assert "Gap number 11" not in text
    assert "- and 2 more: `analytics gaps` lists them all" in text


def test_ten_gaps_are_all_listed_with_no_line_about_more() -> None:
    ten = [gap(number, f"Gap number {number}", questions=30 - number) for number in range(1, 11)]

    text = reports.render(_report([turn(1)], gaps=ten), verbatim=False)

    assert "Gap number 10 (gap 10;" in text
    assert "more: `analytics gaps`" not in text


def test_a_gaps_times_are_printed_on_utcs_clock_whatever_clock_they_arrived_on() -> None:
    """Postgres hands a timestamp back on the clock of the connection's time zone,
    which is the database server's setting. These two arrive three hours ahead of
    UTC: the refresh at noon there, and the last question at half past one in the
    morning, which was still the day before in UTC."""
    ahead = dt.timezone(dt.timedelta(hours=3))
    stored = dataclasses.replace(
        gap(7, "Consulting day rates"),
        refreshed_at=dt.datetime(2026, 10, 12, 12, 0, tzinfo=ahead),
        last_seen_at=dt.datetime(2026, 10, 11, 1, 30, tzinfo=ahead),
    )

    text = reports.render(_report([turn(1)], gaps=[stored]), verbatim=False)

    assert "as of 2026-10-12 09:00 UTC" in text
    assert "last asked 2026-10-10)" in text


def test_a_gap_with_no_question_left_has_not_failed_since_it_was_marked() -> None:
    """Its last question may well be later than the day it was marked written. With
    the question purged there is nothing left that went unanswered, and an alarm
    would point at nothing."""
    marked = {"status": "written", "doc_id": "faq", "resolved_days_ago": 3, "last_seen_days_ago": 1}

    assert gap(1, questions=2, **marked).failed_since_resolved  # type: ignore[arg-type]
    assert not gap(1, questions=0, **marked).failed_since_resolved  # type: ignore[arg-type]


def test_before_any_refresh_the_section_says_how_to_make_one() -> None:
    text = reports.render(_report([turn(1)], gaps=[]), verbatim=True)

    assert "None recorded yet. `analytics refresh-gaps` works them out." in text


def test_a_gap_nobody_asks_about_now_or_that_is_closed_is_not_in_the_queue() -> None:
    """Three that are not work to do: one emptied by the last refresh, one written
    and quiet since, one decided against."""
    report = _report(
        [turn(1)],
        gaps=[
            gap(1, "Nobody asks any more", questions=0, fallbacks=0),
            gap(
                2,
                "Written and quiet",
                status="written",
                doc_id="faq",
                resolved_days_ago=1,
                last_seen_days_ago=5,
            ),
            gap(3, "Decided against", status="wont_fix", resolved_days_ago=9),
        ],
    )

    text = reports.render(report, verbatim=True)

    assert "- no open gap has been asked about lately" in text
    for label in ("Nobody asks any more", "Written and quiet", "Decided against"):
        assert label not in text
    assert "unanswered again since" not in text


def test_a_gap_marked_written_and_unanswered_again_since_is_called_out() -> None:
    """The alarm the status exists for: an article was written to close the gap, and
    the question went unanswered after it. Marked three days ago, asked yesterday."""
    report = _report(
        [turn(1)],
        gaps=[
            gap(
                4,
                "Consulting day rates",
                status="written",
                doc_id="services",
                resolved_days_ago=3,
                last_seen_days_ago=1,
                questions=2,
            ),
        ],
    )

    text = reports.render(report, verbatim=False)

    assert "Marked written, and unanswered again since:" in text
    assert (
        "-    2  Consulting day rates (gap 4; written as services on 2026-10-09; "
        "last asked 2026-10-11)" in text
    )


def test_a_gap_decided_against_is_not_an_alarm_however_often_it_is_asked() -> None:
    report = _report(
        [turn(1)],
        gaps=[gap(5, "Salary expectations", status="wont_fix", resolved_days_ago=9, questions=6)],
    )

    assert "unanswered again since" not in reports.render(report, verbatim=True)
