"""``analytics report`` and ``analytics seed``, as commands.

What each one does is tested where it is written (test_analytics_report.py,
test_analytics_seed.py, and against a real database in tests/integration). This is
the layer around them: which function a flag reaches, what is printed, and the exit
code, which is the only part of a command a scheduler or a script can act on.
``purge`` has the same tests in test_analytics_cli.py.
"""

import datetime as dt
import io
import os
import sys
from decimal import Decimal
from pathlib import Path

import click
import pytest
import structlog
from turns import gap, turn
from typer.testing import CliRunner

from portfolio_ai.analytics import cli, promote, seed
from portfolio_ai.analytics import gaps as gap_refresh
from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.cli import app
from portfolio_ai.analytics.report import Report
from portfolio_ai.analytics.seed import InventedMessage, InventedTraffic, Played, SeedReport
from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db.analytics import ArticleUsage, FailedQuestion, Gap, GapCounts, Volume
from portfolio_ai.evals.datasets import EvalCase
from portfolio_ai.exceptions import AnalyticsError

INVENTED = Path(__file__).resolve().parents[2] / seed.DEFAULT_PATH
QUESTION = "What does Mihail charge for a day of consulting?"


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, click.unstyle(result.output)


# --- report -----------------------------------------------------------------------


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dt.datetime, dt.datetime]]:
    """Stand in for the database and the embeddings call; record the window asked for."""
    windows: list[tuple[dt.datetime, dt.datetime]] = []

    async def build(*, since: dt.datetime, until: dt.datetime) -> Report:  # ruff: ignore[unused-async]
        windows.append((since, until))
        return Report(
            since=since,
            until=until,
            turns=[turn(1, QUESTION, fallback_used=True, top_score=0.31)],
            previous=Volume(questions=0, conversations=0),
            pairs=[],
            refused=[],
            usage=ArticleUsage(searched=1, counted=1, articles=[]),
            gaps=[],
            low_score_threshold=0.35,
            rephrase_similarity=0.60,
            embedded=1,
            embedding_tokens=9,
            embedding_cost=Decimal("0.00000018"),
        )

    monkeypatch.setattr(reports, "build", build)
    return windows


def test_report_prints_the_form_that_quotes(built: list[tuple[dt.datetime, dt.datetime]]) -> None:
    code, output = _invoke("report")

    assert code == 0, output
    assert f"- #1  0.310  {QUESTION}" in output
    [(since, until)] = built
    assert until - since == dt.timedelta(days=7), "seven days when --since is not given"


def test_report_looks_as_far_back_as_it_is_told(
    built: list[tuple[dt.datetime, dt.datetime]],
) -> None:
    code, _ = _invoke("report", "--since", "30d")

    assert code == 0
    [(since, until)] = built
    assert until - since == dt.timedelta(days=30)


def test_a_since_that_is_not_days_fails_without_reading_anything(
    built: list[tuple[dt.datetime, dt.datetime]],
) -> None:
    code, output = _invoke("report", "--since", "last week")

    assert code == 1
    assert built == []
    assert "such as 7d or 30d" in output


def test_a_report_that_could_not_be_built_prints_nothing_and_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build(*, since: dt.datetime, until: dt.datetime) -> Report:  # ruff: ignore[unused-async]
        raise AnalyticsError(f"no report for {since:%Y} to {until:%Y}")

    monkeypatch.setattr(reports, "build", build)

    code, output = _invoke("report")

    assert code == 1
    assert "# Chat report" not in output


# --- seed -------------------------------------------------------------------------


def _result(route: str = "mihail_related", *, fallback: bool = False) -> TurnResult:
    return TurnResult(
        reply="An answer.",
        classification=route,  # type: ignore[arg-type]
        citations=[],
        searches=[],
        retrieved_chunk_ids=[],
        top_score=0.28 if route == "mihail_related" else None,
        fallback_used=fallback,
        links_removed=0,
        calls=[],
        model="a-model",
        latency_ms=4000,
        first_token_ms=3000,
        message_id=2,
    )


@pytest.fixture
def seeding(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[object]]:
    """Replace everything `seed` could do, and record which of them was called."""
    calls: dict[str, list[object]] = {"seed": [], "clear": [], "stored": []}

    async def stored() -> int:  # ruff: ignore[unused-async]
        calls["stored"].append(None)
        return 28

    async def clear() -> seed.Cleared:  # ruff: ignore[unused-async]
        calls["clear"].append(None)
        return seed.Cleared(conversations=28, gaps=13)

    async def run(traffic: InventedTraffic, *, on_played: object = None) -> SeedReport:  # ruff: ignore[unused-async]
        calls["seed"].append(traffic)
        gap = InventedMessage(text="Rates?", kind="gap", topic="rates")
        hello = InventedMessage(text="Hello", kind="small_talk")
        played = [
            Played(conversation="one", message=gap, result=_result(fallback=True)),
            Played(conversation="one", message=hello, result=_result("small_talk")),
        ]
        assert callable(on_played)
        for item in played:
            on_played(item)
        return SeedReport(conversations=1, played=played)

    monkeypatch.setattr(seed, "stored", stored)
    monkeypatch.setattr(seed, "clear", clear)
    monkeypatch.setattr(seed, "seed", run)
    return calls


def test_a_dry_run_prints_the_plan_and_asks_nothing(seeding: dict[str, list[object]]) -> None:
    code, output = _invoke("seed", str(INVENTED), "--dry-run")

    assert code == 0, output
    assert "PLAN (nothing was asked, spent or stored)" in output
    assert "conversations  28" in output
    assert "questions      97" in output
    assert "gap          26" in output
    assert "asked twice    8" in output
    assert "about          $0.10" in output
    assert "28 invented conversation(s) are stored already" in output
    assert seeding["seed"] == []
    assert seeding["clear"] == []


def test_seeding_prints_each_question_as_it_is_answered_and_then_the_totals(
    seeding: dict[str, list[object]],
) -> None:
    code, output = _invoke("seed", str(INVENTED))

    assert code == 0, output
    assert len(seeding["seed"]) == 1
    assert "one                     gap         mihail_related  0.28  fallback" in output
    assert "one                     small_talk  small_talk        -" in output
    assert "SEEDED" in output
    assert "questions      2" in output
    assert "gap           mihail_related            1" in output
    assert "1 gap question(s) got the fallback answer" in output


def test_clear_deletes_and_seeds_nothing(seeding: dict[str, list[object]]) -> None:
    code, output = _invoke("seed", "--clear")

    assert code == 0, output
    assert "28 invented conversation(s) and 13 content gap(s) removed." in output
    assert len(seeding["clear"]) == 1
    assert seeding["seed"] == []


def test_a_dry_run_of_clear_only_counts(seeding: dict[str, list[object]]) -> None:
    code, output = _invoke("seed", "--clear", "--dry-run")

    assert code == 0, output
    assert "28 invented conversation(s) would be removed, and every content gap." in output
    assert seeding["clear"] == []


def test_a_file_that_is_not_there_fails_before_anything_is_asked(
    seeding: dict[str, list[object]], tmp_path: Path
) -> None:
    code, output = _invoke("seed", str(tmp_path / "nowhere.yaml"))

    assert code == 1
    assert "Cannot read" in output
    assert seeding["seed"] == []
    assert seeding["clear"] == []


@pytest.mark.parametrize("flags", [[], ["--dry-run"]], ids=["seed", "dry-run"])
def test_the_environment_is_checked_before_the_file_is_looked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, flags: list[str]
) -> None:
    """The image has no datasets/ folder. Read first, the file would be the complaint
    on the server, and the answer there has to be that this never runs in production,
    not that a file is missing and might be supplied."""

    async def refuse() -> int:  # ruff: ignore[unused-async]
        raise AnalyticsError("ENVIRONMENT is 'production'. Development database only.")

    monkeypatch.setattr(seed, "stored", refuse)

    code, output = _invoke("seed", str(tmp_path / "nowhere.yaml"), *flags)

    assert code == 1
    assert "Development database only" in output
    assert "Cannot read" not in output


def test_a_refusal_is_one_line_and_exit_code_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """What `seed` prints in production. The real refusal is tested in
    test_analytics_seed.py; this is that it reaches the terminal as a message and
    not a traceback."""

    async def refuse() -> int:  # ruff: ignore[unused-async]
        raise AnalyticsError("ENVIRONMENT is 'production'. Development database only.")

    monkeypatch.setattr(seed, "stored", refuse)

    code, output = _invoke("seed", str(INVENTED), "--dry-run")

    assert code == 1
    assert "Development database only" in output
    assert "Traceback" not in output
    assert "PLAN" not in output


def test_the_summary_adds_up_what_was_spent() -> None:
    """SeedReport's own sums, which the summary prints."""
    gap = InventedMessage(text="Rates?", kind="gap", topic="rates")
    report = SeedReport(
        conversations=1,
        played=[Played(conversation="one", message=gap, result=_result(fallback=True))] * 3,
    )

    assert report.routes == {("gap", "mihail_related"): 3}
    assert report.fallbacks == {"gap": 3}
    assert report.cost == Decimal(0), "a result with no calls cost nothing"


def test_a_console_that_cannot_encode_a_character_prints_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Output piped or redirected on Windows is encoded in the ANSI code page. cp1251
    has no non-breaking hyphen, U+2011, which the model writes in place of a plain
    one: a report piped through another program would stop at the first answer that
    had one. The same guard, and the same test, as the evals' commands have."""
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1251"))

    cli._commands()
    print("an API\N{NON-BREAKING HYPHEN}led integration", file=console)
    console.flush()

    assert raw.getvalue() == b"an API?led integration" + os.linesep.encode()


def test_a_promoted_case_is_written_as_utf8_even_where_the_console_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same redirected output as above, for the one command whose output goes
    into a file in the repository. A "?" in a report is a blemish; in a case it is a
    visitor's question changed on its way into a commit. So `promote-case` switches
    its output to UTF-8, and the hyphen arrives as itself.

    The command is run, on a console that is cp1251, and the bytes it wrote are
    read. The first version of this test called the helper that makes the switch,
    which proved the helper and would have passed with the command never calling it.
    """
    question = (
        "an API\N{NON-BREAKING HYPHEN}led integration, "
        "s'il vous pla\N{LATIN SMALL LETTER I WITH CIRCUMFLEX}t"
    )

    async def promoted(  # ruff: ignore[unused-async]
        message_id: int, *, expected_docs: list[str], key: str | None, with_history: bool
    ) -> EvalCase:
        assert (message_id, key, with_history) == (812, None, False)
        return EvalCase(
            key="promoted-812",
            question=question,
            category="mihail_related",
            expected_doc_ids=tuple(expected_docs),
        )

    monkeypatch.setattr(promote, "promote", promoted)

    # charset is the encoding of the console the runner stands in for.
    result = CliRunner(charset="cp1251").invoke(
        app, ["promote-case", "812", "--expected-doc", "services"]
    )

    assert result.exit_code == 0, result.output
    written = result.stdout_bytes.decode("utf-8")
    # No line ending in what is looked for: these are the bytes as written, and on
    # Windows a text stream ends its lines with a carriage return as well.
    assert f"    question: {question}" in written
    assert "?" not in written.split("# One case")[0], "nothing was replaced on the way out"


# --- refresh-gaps -------------------------------------------------------------------

DAY_RATE = "What does Mihail charge for a day of consulting?"


def _group(label: str | None, *, gap_id: int | None, questions: int = 2) -> gap_refresh.Group:
    asked = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)
    failed = [
        FailedQuestion(
            question_id=number,
            question=DAY_RATE,
            query="consulting day rate",
            asked_at=asked,
            top_score=0.3,
            fallback_used=True,
            rating=None,
        )
        for number in range(questions)
    ]
    counts = GapCounts(
        centre=[1.0],
        question_count=questions,
        fallback_count=questions,
        avg_top_score=0.3,
        negative_rate=0.0,
        first_seen_at=asked,
        last_seen_at=asked,
        example_message_ids=[],
    )
    return gap_refresh.Group(failed, counts, gap_id=gap_id, label=label)


@pytest.fixture
def refreshing(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Stand in for the refresh; record whether each call was a dry run."""
    calls: list[bool] = []

    async def refresh(*, dry_run: bool = False) -> gap_refresh.Refresh:  # ruff: ignore[unused-async]
        calls.append(dry_run)
        groups = [
            _group("Consulting day rates", gap_id=7, questions=4),
            _group(None if dry_run else "Salary expectations", gap_id=None, questions=3),
        ]
        return gap_refresh.Refresh(
            failed=7,
            groups=groups,
            emptied=1,
            embedded=6,
            cost=Decimal("0.000412"),
            dry_run=dry_run,
        )

    monkeypatch.setattr(gap_refresh, "refresh", refresh)
    return calls


def test_refresh_gaps_prints_labels_and_counts_and_never_a_question(
    refreshing: list[bool],
) -> None:
    """`digest` runs the same refresh on a Monday, where output is the log. So this
    command's own output is held to the same rule, and `gaps` is the one that quotes."""
    code, output = _invoke("refresh-gaps")

    assert code == 0, output
    assert refreshing == [False]
    assert "GAPS REFRESHED" in output
    assert "failed questions  7" in output
    assert "new             1" in output
    assert "known           1" in output
    assert "emptied           1" in output
    assert "cost              $0.000412" in output
    assert "     4  known  Consulting day rates" in output
    assert "     3  new    Salary expectations" in output
    assert DAY_RATE not in output
    assert "consulting day rate" not in output, "nor the search, which is a rewording of it"


def test_a_dry_run_of_the_refresh_names_nothing(refreshing: list[bool]) -> None:
    code, output = _invoke("refresh-gaps", "--dry-run")

    assert code == 0, output
    assert refreshing == [True]
    assert "PLAN (nothing was named or written)" in output
    assert "     3  new    (not named yet)" in output
    assert "would empty       1" in output, "a plan says what would happen, not what did"
    assert "emptied  " not in output


def test_a_refresh_says_when_it_left_questions_out_and_gaps_unnamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refresh(*, dry_run: bool = False) -> gap_refresh.Refresh:  # ruff: ignore[unused-async]
        return gap_refresh.Refresh(
            failed=gap_refresh.MAX_QUESTIONS,
            groups=[
                _group("Consulting day rates", gap_id=None, questions=4),
                _group(gap_refresh.UNNAMED, gap_id=None, questions=1),
            ],
            emptied=0,
            embedded=5,
            cost=Decimal("0.000150"),
            dry_run=dry_run,
            capped=True,
        )

    monkeypatch.setattr(gap_refresh, "refresh", refresh)

    code, output = _invoke("refresh-gaps")

    assert code == 0, output
    assert "more are stored: the newest 500 were grouped" in output
    assert "not named yet   1: the next refresh asks again" in output
    assert "     1  new    Unnamed gap" in output


def test_a_refresh_that_left_nothing_out_says_nothing_about_it(refreshing: list[bool]) -> None:
    _, output = _invoke("refresh-gaps")

    assert refreshing == [False]
    assert "more are stored" not in output
    assert "not named yet" not in output


def test_a_refresh_that_fails_prints_no_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refresh(*, dry_run: bool = False) -> gap_refresh.Refresh:  # ruff: ignore[unused-async]
        raise AnalyticsError(f"no refresh (dry_run={dry_run})")

    monkeypatch.setattr(gap_refresh, "refresh", refresh)

    code, output = _invoke("refresh-gaps")

    assert code == 1
    assert "GAPS REFRESHED" not in output


# --- gaps ---------------------------------------------------------------------------


@pytest.fixture
def looked_up() -> list[list[int]]:
    """The ids each lookup of example questions asked for."""
    return []


@pytest.fixture
def stored_gaps(monkeypatch: pytest.MonkeyPatch, looked_up: list[list[int]]) -> list[str | None]:
    """Three gaps in the table, and all but two of their example questions still
    stored. Records the status each listing asked for."""
    asked_for: list[str | None] = []
    table = [
        gap(7, "Consulting day rates", questions=5, fallbacks=3, examples=(11, 13, 15, 17, 19)),
        gap(2, "Salary expectations", status="wont_fix", resolved_days_ago=9, examples=(21,)),
        gap(
            4,
            "Client references",
            status="written",
            doc_id="services",
            resolved_days_ago=3,
            last_seen_days_ago=1,
            examples=(31,),
        ),
    ]

    async def list_gaps(*, status: str | None = None) -> list[Gap]:  # ruff: ignore[unused-async]
        asked_for.append(status)
        return [item for item in table if status in {None, item.status}]

    async def questions_by_id(question_ids: list[int]) -> dict[int, str]:  # ruff: ignore[unused-async]
        looked_up.append(list(question_ids))
        # 13 and 21 went with the purge.
        kept = {
            11: DAY_RATE,
            15: "How much per day?",
            17: "Is there a price list?",
            19: "What would a week cost?",
            31: "Any references?",
        }
        return {number: kept[number] for number in question_ids if number in kept}

    monkeypatch.setattr(analytics_db, "list_gaps", list_gaps)
    monkeypatch.setattr(analytics_db, "questions_by_id", questions_by_id)
    return asked_for


def test_gaps_lists_the_open_ones_with_the_questions_still_stored(
    stored_gaps: list[str | None],
) -> None:
    code, output = _invoke("gaps")

    assert code == 0, output
    assert stored_gaps == ["open"], "open unless told otherwise"
    assert "#7  Consulting day rates  [open]" in output
    assert "5 question(s), 3 said the information was not there, average best score 0.310" in output
    assert "first asked 2026-09-22, last 2026-10-11" in output
    # Three examples at most, in the gap's own order, skipping the one that is gone.
    # Four of this gap's five are still stored, and the fourth is not printed.
    # Each with its id, which is what `promote-case` takes once the article exists.
    examples = [line.strip() for line in output.splitlines() if line.startswith("      - ")]
    assert examples == [
        f"- #11  {DAY_RATE}",
        "- #15  How much per day?",
        "- #17  Is there a price list?",
    ]
    assert "What would a week cost?" not in output
    assert "Salary expectations" not in output


def test_all_lists_every_gap_and_says_what_was_decided_about_each(
    stored_gaps: list[str | None], looked_up: list[list[int]]
) -> None:
    code, output = _invoke("gaps", "--status", "all")

    assert code == 0, output
    assert stored_gaps == [None]
    assert looked_up == [[11, 13, 15, 17, 19, 21, 31]], (
        "one lookup for every gap's examples, and not one for each gap"
    )
    assert "#2  Salary expectations  [wont_fix]" in output
    assert "    marked wont_fix on 2026-10-03" in output
    assert "#4  Client references  [written]" in output
    assert "    marked written on 2026-10-09, closed by services" in output
    assert "    asked again, and unanswered, since then" in output
    assert output.count("asked again, and unanswered") == 1, (
        "only the one asked after it was marked"
    )


def test_a_status_filters_the_list(stored_gaps: list[str | None]) -> None:
    code, output = _invoke("gaps", "--status", "wont_fix")

    assert code == 0, output
    assert stored_gaps == ["wont_fix"]
    assert "Salary expectations" in output
    assert "Consulting day rates" not in output


def test_no_gaps_says_how_they_are_made(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_gaps(*, status: str | None = None) -> list[Gap]:  # ruff: ignore[unused-async]
        assert status == "open"
        return []

    monkeypatch.setattr(analytics_db, "list_gaps", list_gaps)

    code, output = _invoke("gaps")

    assert code == 0
    assert "No gaps to list. `refresh-gaps` works them out" in output


# --- gap-status ---------------------------------------------------------------------


@pytest.fixture
def marking(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str, str | None]]:
    """Gap 7 exists and the article `services` is indexed. Records each change made."""
    made: list[tuple[int, str, str | None]] = []

    async def set_gap_status(gap_id: int, status: str, *, doc_id: str | None) -> bool:  # ruff: ignore[unused-async]
        if gap_id != 7:
            return False
        made.append((gap_id, status, doc_id))
        return True

    async def document_exists(doc_id: str) -> bool:  # ruff: ignore[unused-async]
        return doc_id == "services"

    monkeypatch.setattr(analytics_db, "set_gap_status", set_gap_status)
    monkeypatch.setattr(analytics_db, "document_exists", document_exists)
    return made


def test_a_gap_is_marked_written_with_the_article_that_closes_it(
    marking: list[tuple[int, str, str | None]],
) -> None:
    code, output = _invoke("gap-status", "7", "written", "--doc", "services")

    assert code == 0, output
    assert marking == [(7, "written", "services")]
    assert "Gap 7 is now written, closed by services." in output


@pytest.mark.parametrize("status", ["wont_fix", "open"])
def test_a_gap_is_decided_against_or_opened_again_with_no_article(
    marking: list[tuple[int, str, str | None]], status: str
) -> None:
    code, output = _invoke("gap-status", "7", status)

    assert code == 0, output
    assert marking == [(7, status, None)]
    assert f"Gap 7 is now {status}." in output


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        (["7", "written"], "`written` needs --doc"),
        (
            ["7", "written", "--doc", "not-indexed"],
            "No indexed article has the doc_id 'not-indexed'",
        ),
        (["7", "wont_fix", "--doc", "services"], "--doc goes with `written` only"),
        (["7", "open", "--doc", "services"], "--doc goes with `written` only"),
        (["99", "wont_fix"], "There is no gap 99"),
    ],
    ids=[
        "written-without-doc",
        "an-article-not-indexed",
        "doc-with-wont-fix",
        "doc-with-open",
        "no-such-gap",
    ],
)
def test_a_change_that_makes_no_sense_is_refused_and_changes_nothing(
    marking: list[tuple[int, str, str | None]], arguments: list[str], says: str
) -> None:
    code, output = _invoke("gap-status", *arguments)

    assert code == 1
    assert says in output
    assert marking == []
    assert "is now" not in output


def test_a_status_that_does_not_exist_is_a_usage_error(
    marking: list[tuple[int, str, str | None]],
) -> None:
    code, _ = _invoke("gap-status", "7", "done")

    assert code == 2, "Typer's own refusal, before the command runs"
    assert marking == []


# --- promote-case -------------------------------------------------------------------


@pytest.fixture
def promoting(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Stand in for the database reads; record what each call was asked for."""
    calls: list[dict[str, object]] = []

    async def promoted(  # ruff: ignore[unused-async]
        message_id: int, *, expected_docs: list[str], key: str | None, with_history: bool
    ) -> EvalCase:
        calls.append({"id": message_id, "docs": expected_docs, "key": key, "history": with_history})
        # What the real reads do on the way: the pool logs a line when it opens. At
        # the usual level that line would be printed above the case.
        structlog.get_logger("portfolio_ai.db.pool").info("pool_opened", max_size=10)
        return EvalCase(
            key=key or "promoted-812",
            question=DAY_RATE,
            category="mihail_related",
            expected_doc_ids=tuple(expected_docs),
        )

    monkeypatch.setattr(promote, "promote", promoted)
    return calls


def test_promote_case_prints_the_case_and_then_what_to_do_with_it(
    promoting: list[dict[str, object]],
) -> None:
    code, output = _invoke("promote-case", "812", "--expected-doc", "services")

    assert code == 0, output
    assert promoting == [{"id": 812, "docs": ["services"], "key": None, "history": False}]
    # The case is the first thing printed. The stand-in logged a line at INFO, as the
    # pool does: the command runs at WARNING so that no such line comes before it, and
    # all of the output can be pasted.
    assert output.startswith("  - key: promoted-812\n")
    assert "pool_opened" not in output
    assert f"    question: {DAY_RATE}\n" in output
    assert "    expected_doc_ids:\n    - services\n" in output
    # Everything after the case is a comment, so the output can be pasted whole.
    after = output[output.index("# One case") :]
    assert all(line.startswith("#") for line in after.splitlines())
    assert "to go under `cases:` in datasets/promoted.yaml" in after


def test_every_flag_of_promote_case_reaches_the_function(
    promoting: list[dict[str, object]],
) -> None:
    code, output = _invoke(
        "promote-case",
        "812",
        "--expected-doc",
        "services",
        "--expected-doc",
        "faq",
        "--key",
        "day-rate-pricing",
        "--with-history",
    )

    assert code == 0, output
    assert promoting == [
        {"id": 812, "docs": ["services", "faq"], "key": "day-rate-pricing", "history": True}
    ]
    assert "  - key: day-rate-pricing\n" in output


def test_promote_case_without_a_document_is_a_usage_error(
    promoting: list[dict[str, object]],
) -> None:
    """A case promoted here names the article written to answer it. Typer refuses the
    command before anything is read: exit code 2, which is a usage error's."""
    code, output = _invoke("promote-case", "812")

    assert code == 2
    assert "--expected-doc" in output
    assert promoting == []


def test_a_promotion_that_is_refused_prints_no_case(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refused(  # ruff: ignore[unused-async]
        message_id: int, *, expected_docs: list[str], key: str | None, with_history: bool
    ) -> EvalCase:
        asked = (expected_docs, key, with_history)
        raise AnalyticsError(f"No answered question has the id {message_id} {asked}.")

    monkeypatch.setattr(promote, "promote", refused)

    code, output = _invoke("promote-case", "999", "--expected-doc", "services")

    assert code == 1
    assert "No answered question has the id 999" in output
    assert "- key:" not in output
    assert "# One case" not in output
