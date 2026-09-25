"""The command line: what each command refuses, and what a dry run leaves untouched.

The database functions the commands read are replaced; the runner's own behaviour
is tested in test_eval_runner.py. Logging is left alone too: configuring it for the
process would take over the test runner's capture for every test after these.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from portfolio_ai.config import get_settings
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import Corpus, RunRecord
from portfolio_ai.evals import cli, datasets, runner

GOLDEN = Path(__file__).resolve().parents[2] / "datasets" / "golden_v1.yaml"


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
    assert "golden_v1: " in result.output
    assert "valid" in result.output


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
    assert "chat        gpt-5-mini, effort default" in result.output
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
    assert "chat        gpt-5-mini, effort default" in forced.output


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
