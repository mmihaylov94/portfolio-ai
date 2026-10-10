"""The command line: what each command refuses, and what a dry run leaves untouched.

The database functions the commands read are replaced; the runner's own behaviour
is tested in test_eval_runner.py. Logging is left alone too: configuring it for the
process would take over the test runner's capture for every test after these.
"""

import asyncio
import dataclasses
import datetime as dt
import inspect
import io
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from portfolio_ai.config import get_settings
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import Corpus, ResultRow, RunRecord
from portfolio_ai.evals import cli, datasets, runner

GOLDEN = Path(__file__).resolve().parents[2] / "datasets" / "golden_v2.yaml"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "configure_logging", lambda level: None)  # ruff: ignore[unused-lambda-argument]


@pytest.fixture
def indexed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A database that has every document the golden set expects, and no runs."""

    async def label_taken(label: str) -> bool:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        return False

    async def corpus_state() -> Corpus:
        await asyncio.sleep(0)
        cases = datasets.load(GOLDEN).cases
        doc_ids = frozenset(doc for case in cases for doc in case.expected_doc_ids)
        return Corpus(documents=11, chunks=111, fingerprint="0123456789ab", doc_ids=doc_ids)

    async def stored_dataset(name: str) -> None:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)

    monkeypatch.setattr(evals_db, "label_taken", label_taken)
    monkeypatch.setattr(evals_db, "corpus_state", corpus_state)
    monkeypatch.setattr(evals_db, "stored_dataset", stored_dataset)


def test_check_summarises_a_valid_dataset() -> None:
    result = CliRunner().invoke(cli.app, ["check", str(GOLDEN)])

    assert result.exit_code == 0, result.output
    assert "golden_v2: " in result.output
    assert "valid" in result.output


def _newest_golden() -> str:
    """The highest-numbered ``golden_v<N>.yaml``. A draft is kept under another name
    until it is reviewed, so it is not a version yet and is not counted here."""
    versions = {
        int(match.group(1)): path.stem
        for path in GOLDEN.parent.glob("*.yaml")
        if (match := re.fullmatch(r"golden_v(\d+)", path.stem))
    }
    return versions[max(versions)]


def test_a_run_defaults_to_the_newest_golden_dataset() -> None:
    """A forgotten --dataset must not grade today's answers against an older knowledge
    base: that run is paid for, and it marks right answers wrong."""
    assert inspect.signature(cli.run).parameters["dataset"].default == _newest_golden()


@pytest.mark.parametrize("document", ["README.md", "CLAUDE.md", "docs/EVALS.md"])
def test_the_documented_check_command_names_the_default_dataset(document: str) -> None:
    """When the default moved to golden_v2 the README went on validating golden_v1, then
    planned a run of golden_v2, then compared it with a golden_v1 run: a paid run and a
    refusal, from three commands printed together."""
    text = (GOLDEN.parents[1] / document).read_text(encoding="utf-8")

    named = set(re.findall(r"evals check datasets/(golden_v\d+)\.yaml", text))

    assert named == {_newest_golden()}


def test_check_names_what_is_wrong_with_a_broken_one(tmp_path: Path) -> None:
    broken = tmp_path / "golden_v1.yaml"
    broken.write_text(
        "name: golden_v1\ndescription: x\ncases:\n  - {key: a, question: q, category: banter}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli.app, ["check", str(broken)])

    assert result.exit_code == 1
    assert "cases.0.category" in result.output


def test_a_run_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evals spend money and write rows: the development database only."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()

    result = CliRunner().invoke(cli.app, ["run", "--label", "baseline", "--dry-run"])

    assert result.exit_code != 0
    assert "ENVIRONMENT is 'production'" in result.output


@pytest.mark.usefixtures("indexed")
def test_a_dry_run_prints_the_plan_and_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def must_not_run(*args: Any, **kwargs: Any) -> None:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        raise AssertionError("a dry run executed the plan")

    monkeypatch.setattr(runner, "execute", must_not_run)

    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--dataset",
            str(GOLDEN),
            "--label",
            "baseline",
            "--chat-effort",
            "default",
            "--top-k",
            "8",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "chat        gpt-6-luna, effort default" in result.output
    assert "top_k 8" in result.output
    assert "11 documents, 111 chunks" in result.output
    assert "dataset     new: stored when the run starts" in result.output
    assert "nothing was run, spent or stored" in result.output


@pytest.mark.usefixtures("indexed")
def test_the_effort_given_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_REASONING_EFFORT", "high")
    get_settings.cache_clear()

    inherited = CliRunner().invoke(
        cli.app, ["run", "--dataset", str(GOLDEN), "--label", "a", "--dry-run"]
    )
    forced = CliRunner().invoke(
        cli.app,
        ["run", "--dataset", str(GOLDEN), "--label", "b", "--chat-effort", "default", "--dry-run"],
    )

    assert "effort high" in inherited.output
    assert "chat        gpt-6-luna, effort default" in forced.output


@pytest.mark.usefixtures("indexed")
def test_a_gpt_6_model_is_planned_with_its_own_lowest_effort() -> None:
    """Two things had to exist first: a price for the model, without which a run is
    refused, and "none" as an effort, which is not the same as sending no effort."""
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            "--dataset",
            str(GOLDEN),
            "--label",
            "luna",
            "--chat-model",
            "gpt-6-luna",
            "--chat-effort",
            "none",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "chat        gpt-6-luna, effort none" in result.output


def test_showing_a_run_that_does_not_exist_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    async def find_run(label: str) -> RunRecord | None:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(evals_db, "find_run", find_run)

    result = CliRunner().invoke(cli.app, ["show", "nope"])

    assert result.exit_code == 1
    assert "There is no run labelled 'nope'" in result.output


def test_listing_before_any_run(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_runs() -> list[RunRecord]:
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr(evals_db, "list_runs", list_runs)

    result = CliRunner().invoke(cli.app, ["list"])

    assert result.exit_code == 0
    assert "No runs yet." in result.output


def _stored_run(label: str, judge: str, rubric: str, **config: Any) -> RunRecord:
    return RunRecord(
        id=3,
        label=label,
        dataset_id=1,
        dataset="golden_v2",
        status="complete",
        config={"judge": {"model": judge, "prompt": rubric}, **config},
        totals={"classification": {"accuracy": 1.0}, "judge": {"faithfulness": 4.92}},
        started_at=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
        finished_at=None,
    )


def test_a_regrade_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """It spends money and writes a run, as `run` does."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()

    result = CliRunner().invoke(cli.app, ["rejudge", "v2-baseline", "--label", "x", "--dry-run"])

    assert result.exit_code != 0
    assert "ENVIRONMENT is 'production'" in result.output


@pytest.mark.usefixtures("indexed")
def test_a_regrade_dry_run_prints_the_plan_and_grades_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _stored_run(
        "v2-baseline",
        "gpt-5",
        "judge@1",
        corpus={"fingerprint": "0123456789ab"},
        dataset={"content_hash": datasets.load(GOLDEN).content_hash},
    )

    async def find_run(label: str) -> RunRecord | None:
        await asyncio.sleep(0)
        return source if label == "v2-baseline" else None

    async def stored_results(run_id: int) -> list[Any]:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        return []

    async def must_not_run(*args: Any, **kwargs: Any) -> None:  # ruff: ignore[unused-function-argument]
        await asyncio.sleep(0)
        raise AssertionError("a dry run graded something")

    monkeypatch.setattr(evals_db, "find_run", find_run)
    monkeypatch.setattr(evals_db, "stored_results", stored_results)
    # The source run names its dataset; wherever the tests run from, it is this file.
    monkeypatch.setattr(datasets, "path_for", lambda name: GOLDEN)  # ruff: ignore[unused-lambda-argument]
    monkeypatch.setattr(runner, "rejudge", must_not_run)

    result = CliRunner().invoke(
        cli.app,
        [
            "rejudge",
            "v2-baseline",
            "--label",
            "v2-baseline-sol",
            "--judge-model",
            "gpt-6.1-sol",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "v2-baseline-sol: 0 results of v2-baseline (golden_v2)" in result.output
    assert "judge       gpt-6.1-sol judge@" in result.output
    assert "was         gpt-5 judge@1" in result.output
    assert "nothing was graded, spent or stored" in result.output


def _graded_answer(faithfulness: int) -> ResultRow:
    """One knowledge-base answer as `compare` reads it back, with the judge's score."""
    return ResultRow(
        case_id=1,
        key="job-title",
        question="What is Mihail's job title?",
        category="mihail_related",
        also_accept=[],
        expected_doc_ids=["about-mihail"],
        expect_fallback=False,
        answer="He is a Solutions Architect.",
        route="mihail_related",
        retrieved_doc_ids=["about-mihail"],
        recall=1.0,
        precision=1.0,
        mrr=1.0,
        judge_scores={
            "faithfulness": faithfulness,
            "completeness": 5,
            "style": 5,
            "declined": False,
        },
        judge_rationale="Scripted.",
        violations={},
        prompt_tokens=1000,
        completion_tokens=100,
        cost=Decimal("0.004"),
        judge_cost=Decimal("0.02"),
        latency_ms=10_000,
        first_token_ms=8_000,
        top_score=0.7,
        fallback_used=False,
        calls=[],
        error=None,
    )


def test_comparing_runs_graded_by_different_judges_says_so_and_shows_no_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same answer, scored 5 by one judge and 2 by the other. Under one judge that
    is a case that got worse; under two it is two rulers, and it is not listed: not in
    the table, not in the case list, and not in the Markdown file either."""
    runs = {
        "v2-baseline": _stored_run("v2-baseline", "gpt-5", "judge@1"),
        "v2-baseline-sol": dataclasses.replace(
            _stored_run("v2-baseline-sol", "gpt-6.1-sol", "judge@2"), id=4
        ),
    }
    rows = {3: [_graded_answer(5)], 4: [_graded_answer(2)]}

    async def find_run(label: str) -> RunRecord | None:
        await asyncio.sleep(0)
        return runs.get(label)

    async def results(run_id: int) -> list[ResultRow]:
        await asyncio.sleep(0)
        return rows[run_id]

    monkeypatch.setattr(evals_db, "find_run", find_run)
    monkeypatch.setattr(evals_db, "results", results)
    written = tmp_path / "compare.md"

    result = CliRunner().invoke(
        cli.app, ["compare", "v2-baseline", "v2-baseline-sol", "--markdown", str(written)]
    )

    assert result.exit_code == 0, result.output
    assert (
        "v2-baseline was graded by gpt-5 judge@1 and v2-baseline-sol by gpt-6.1-sol judge@2."
        in result.output
    )
    assert "faithfulness" not in result.output
    assert "classification" in result.output
    assert "worse in the second run (0)" in result.output
    markdown = written.read_text(encoding="utf-8")
    assert markdown.startswith("v2-baseline was graded by gpt-5 judge@1")
    assert "faithfulness" not in markdown
    assert "worse in the second run (0)" in markdown


def test_a_console_that_cannot_encode_a_character_prints_a_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Redirected output on Windows is encoded in the ANSI code page, and cp1251 has
    # no U+2011, the non-breaking hyphen a real answer carried; before the fix,
    # printing it ended `show` with a traceback.
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1251")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1251"))

    cli._commands()
    print("gpt\N{NON-BREAKING HYPHEN}5-mini", file=console)
    console.flush()

    assert raw.getvalue() == b"gpt?5-mini" + os.linesep.encode()
