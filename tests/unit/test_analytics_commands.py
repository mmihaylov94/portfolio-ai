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
from turns import turn
from typer.testing import CliRunner

from portfolio_ai.analytics import cli, seed
from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.cli import app
from portfolio_ai.analytics.report import Report
from portfolio_ai.analytics.seed import InventedMessage, InventedTraffic, Played, SeedReport
from portfolio_ai.assistant.agent import TurnResult
from portfolio_ai.db.analytics import ArticleUsage, Volume
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

    async def clear() -> int:  # ruff: ignore[unused-async]
        calls["clear"].append(None)
        return 28

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
    assert "28 invented conversation(s) removed." in output
    assert len(seeding["clear"]) == 1
    assert seeding["seed"] == []


def test_a_dry_run_of_clear_only_counts(seeding: dict[str, list[object]]) -> None:
    code, output = _invoke("seed", "--clear", "--dry-run")

    assert code == 0, output
    assert "28 invented conversation(s) would be removed." in output
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
