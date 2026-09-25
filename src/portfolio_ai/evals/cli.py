"""``python -m portfolio_ai.evals`` -- score the assistant against a golden dataset.

::

    check datasets/golden_v1.yaml           validate a dataset file; no database, no network
    run --dataset golden_v1 --label baseline [--dry-run] [knobs]
    list                                    every run so far
    show baseline [--failures]              one run, case by case
    compare baseline effort-low             two runs side by side, and what changed

Local only. A run spends money and writes to the eval tables, and none of that
belongs in production: the evals measure the code and the knowledge base, which are
the same on the development machine, against the development database.

Run it from the repository root, which is where ``--dataset golden_v1`` finds
``datasets/golden_v1.yaml``.
"""

import asyncio
import dataclasses
import io
import sys
from collections import Counter
from collections.abc import Callable, Coroutine
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from portfolio_ai.assistant.agent import AssistantConfig
from portfolio_ai.config import ReasoningEffort, get_settings
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import RunRecord
from portfolio_ai.db.pool import close_pool
from portfolio_ai.evals import datasets, report, runner
from portfolio_ai.evals.runner import Plan, Progress
from portfolio_ai.exceptions import EvalError, PortfolioAIError
from portfolio_ai.llm.client import close_client
from portfolio_ai.logging import configure_logging

app = typer.Typer(add_completion=False, help="Score the assistant against a golden dataset.")

DIM = typer.colors.BRIGHT_BLACK


class Effort(StrEnum):
    """A reasoning effort as the command line spells it.

    ``default`` is the one addition: it sends no effort at all, so the model's own
    default applies -- which is what n8n sent, and what a baseline has to be run at
    even when the local .env sets something else.
    """

    DEFAULT = "default"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _effort(choice: Effort | None, current: ReasoningEffort | None) -> ReasoningEffort | None:
    if choice is None:
        return current
    if choice is Effort.DEFAULT:
        return None
    # The remaining members' values are exactly the four ReasoningEffort spells;
    # cast() tells mypy so, without changing anything at run time.
    return cast("ReasoningEffort", choice.value)


@app.callback()
def _commands() -> None:
    """Score the assistant against a golden dataset."""
    # On Windows, output redirected to a file or piped is encoded in the ANSI code
    # page (cp1251, cp1252...), and Python raises UnicodeEncodeError for any
    # character outside it -- which a model's answer supplies freely (a
    # non-breaking hyphen, U+2011, stopped the first real `show` halfway). An
    # interactive console is UTF-8 since Python 3.6 and never hits this.
    # "replace" prints "?" for those instead. isinstance, because sys.stdout is
    # typed as TextIO, which has no reconfigure(), and could be a StringIO.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


def _fail(message: str) -> typer.Exit:
    typer.secho(message, fg=typer.colors.RED, err=True)
    return typer.Exit(1)


async def _shutdown() -> None:
    await close_client()
    await close_pool()


def _in_loop[T](work: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run ``work`` on one event loop, and close the connections afterwards.

    One loop for everything a command does: the pool binds to the loop that opened
    it, so preparing on one loop and running on another would fail.

    ``work`` is a function that makes the coroutine, not the coroutine itself -- the
    same reason ``cli.run_async`` gives: a coroutine created and then never run
    because something failed first is reported as "never awaited".
    """
    with asyncio.Runner() as session:
        try:
            return session.run(work())
        except PortfolioAIError as exc:
            raise _fail(str(exc)) from exc
        finally:
            session.run(_shutdown())


# --- check ----------------------------------------------------------------------


@app.command()
def check(path: Annotated[Path, typer.Argument(help="The dataset file.")]) -> None:
    """Validate a dataset file. Reads nothing else and calls nothing."""
    try:
        dataset = datasets.load(path)
    except EvalError as exc:
        raise _fail(str(exc)) from exc

    typer.echo(f"{dataset.name}: {len(dataset.cases)} cases, valid")
    for category, count in sorted(Counter(case.category for case in dataset.cases).items()):
        typer.echo(f"  {category:<18}{count}")
    typer.echo(f"  {'expect fallback':<18}{sum(case.expect_fallback for case in dataset.cases)}")
    typer.echo(f"  {'with history':<18}{sum(bool(case.history) for case in dataset.cases)}")


# --- run ------------------------------------------------------------------------


def _print_plan(plan: Plan) -> None:
    config = plan.config
    judged = sum(case.category != "out_of_scope" for case in plan.cases)
    typer.echo(f"\n{plan.label}: {len(plan.cases)} cases from {plan.dataset.name}")
    typer.echo(f"  chat        {config.chat_model}, effort {config.chat_effort or 'default'}")
    typer.echo(
        f"  classifier  {config.classifier_model}, effort {config.classifier_effort or 'default'}"
    )
    typer.echo(f"  retrieval   top_k {config.top_k}, up to {config.max_search_rounds} searches")
    typer.echo(
        f"  judge       {plan.judge_model or 'none'}"
        + (f", about {judged} answers to grade" if plan.judge_model else "")
    )
    typer.echo(
        f"  corpus      {plan.corpus.documents} documents, {plan.corpus.chunks} chunks, "
        f"{plan.corpus.fingerprint}"
    )
    typer.echo(f"  dataset     {plan.dataset_state}")
    typer.echo(f"  at once     {plan.concurrency}\n")


def _print_progress(progress: Progress) -> None:
    result = progress.result
    route = result.classification or "unanswered"
    scores = result.judge_scores or {}
    grades = " ".join(
        f"{name[0].upper()}{scores.get(name) if scores.get(name) is not None else '-'}"
        for name in report.JUDGED
    )
    took = f"{result.latency_ms / 1000:.1f}s" if result.latency_ms is not None else "-"
    problems = [*result.violations, *(["error"] if result.error else [])]
    line = (
        f"[{progress.done:>3}/{progress.total}] {progress.key:<32}{route:<16}{grades:<14}{took:>7}"
    )
    typer.echo(line + (f"  {', '.join(problems)}" if problems else ""))


@app.command()
def run(  # ruff: ignore[too-many-arguments] -- one per knob, and every one is named
    *,
    label: Annotated[str, typer.Option("--label", help="A name for this run, unique.")],
    dataset: Annotated[
        str, typer.Option("--dataset", help="A name in datasets/, or a path.")
    ] = "golden_v1",
    chat_model: Annotated[str | None, typer.Option(help="Default: CHAT_MODEL.")] = None,
    classifier_model: Annotated[str | None, typer.Option(help="Default: CLASSIFIER_MODEL.")] = None,
    chat_effort: Annotated[
        Effort | None, typer.Option(help="Default: CHAT_REASONING_EFFORT.")
    ] = None,
    classifier_effort: Annotated[
        Effort | None, typer.Option(help="Default: CLASSIFIER_REASONING_EFFORT.")
    ] = None,
    top_k: Annotated[
        int | None, typer.Option(min=1, max=100, help="Default: RETRIEVAL_TOP_K.")
    ] = None,
    max_search_rounds: Annotated[
        int | None, typer.Option(min=1, max=10, help="Default: AGENT_MAX_SEARCH_ROUNDS.")
    ] = None,
    judge_model: Annotated[str | None, typer.Option(help="Default: JUDGE_MODEL.")] = None,
    no_judge: Annotated[
        bool, typer.Option("--no-judge", help="Skip grading: cheaper, and no quality scores.")
    ] = False,
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Run just this case. Repeatable.")
    ] = None,
    concurrency: Annotated[
        int, typer.Option(min=1, max=10, help="Cases answered at once.")
    ] = runner.DEFAULT_CONCURRENCY,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check everything and stop. Spends nothing.")
    ] = False,
) -> None:
    """Answer and grade every case in a dataset, and store the run."""
    settings = get_settings()
    if settings.environment != "local":
        raise typer.BadParameter(
            f"ENVIRONMENT is {settings.environment!r}. Evals run against the development "
            "database only: they spend money and write to the eval tables.",
            param_hint="ENVIRONMENT",
        )

    # Warnings only: a line per OpenAI call would bury the progress lines.
    configure_logging("WARNING")

    base = AssistantConfig.from_settings(settings)
    config = dataclasses.replace(
        base,
        chat_model=chat_model or base.chat_model,
        classifier_model=classifier_model or base.classifier_model,
        chat_effort=_effort(chat_effort, base.chat_effort),
        classifier_effort=_effort(classifier_effort, base.classifier_effort),
        top_k=top_k or base.top_k,
        max_search_rounds=max_search_rounds or base.max_search_rounds,
    )

    try:
        loaded = datasets.load(datasets.path_for(dataset))
    except EvalError as exc:
        raise _fail(str(exc)) from exc

    async def work() -> RunRecord | None:
        plan = await runner.prepare(
            loaded,
            label=label,
            config=config,
            judge_model=None if no_judge else judge_model or settings.judge_model,
            concurrency=concurrency,
            only=frozenset(only or ()),
        )
        _print_plan(plan)
        if dry_run:
            return None
        return await runner.execute(plan, on_result=_print_progress)

    record = _in_loop(work)
    if record is None:
        typer.echo("Dry run: every check passed, and nothing was run, spent or stored.")
        return

    typer.echo("")
    for line in [*report.describe(record), "", *report.headline([record])]:
        typer.echo(line)
    typer.secho(f"\nCase by case: python -m portfolio_ai.evals show {label} --failures", fg=DIM)


# --- reading runs back ----------------------------------------------------------


@app.command("list")
def list_runs() -> None:
    """Every run so far, oldest first."""
    runs = _in_loop(evals_db.list_runs)
    if not runs:
        typer.echo("No runs yet.")
        return

    typer.echo(
        f"{'label':<24}{'dataset':<14}{'started':<18}{'status':<10}"
        f"{'cases':>6}{'route':>7}{'hit':>7}{'F':>6}{'C':>6}{'S':>6}{'first p50':>11}"
    )
    for run_record in runs:
        totals = run_record.totals or {}
        judge = totals.get("judge", {})
        first = (totals.get("first_token_ms", {}).get("mihail_related") or {}).get("p50")

        def number(value: object, decimals: int = 2) -> str:
            return f"{value:.{decimals}f}" if isinstance(value, (int, float)) else "-"

        typer.echo(
            f"{run_record.label:<24}{run_record.dataset:<14}"
            f"{run_record.started_at:%Y-%m-%d %H:%M}  {run_record.status:<10}"
            f"{totals.get('cases', '-'):>6}"
            f"{number(totals.get('classification', {}).get('accuracy')):>7}"
            f"{number(totals.get('retrieval', {}).get('hit_rate')):>7}"
            f"{number(judge.get('faithfulness')):>6}{number(judge.get('completeness')):>6}"
            f"{number(judge.get('style')):>6}"
            f"{number(first / 1000 if isinstance(first, int) else None, 1):>10}s"
        )


async def _run_and_rows(label: str) -> tuple[RunRecord, list[evals_db.ResultRow]]:
    record = await evals_db.find_run(label)
    if record is None:
        raise EvalError(f"There is no run labelled {label!r}. `list` shows the runs there are.")
    return record, await evals_db.results(record.id)


@app.command()
def show(
    label: Annotated[str, typer.Argument(help="The run's label.")],
    failures: Annotated[
        bool, typer.Option("--failures", help="Only the cases with a problem, explained.")
    ] = False,
) -> None:
    """One run: its configuration, its totals, and every case."""
    record, rows = _in_loop(lambda: _run_and_rows(label))
    for line in [
        *report.describe(record),
        "",
        *report.headline([record]),
        "",
        *report.cases(rows, failures_only=failures),
    ]:
        typer.echo(line)


@app.command()
def compare(
    first: Annotated[str, typer.Argument(help="The run to compare against, usually the baseline.")],
    second: Annotated[str, typer.Argument(help="The run being judged.")],
    markdown: Annotated[
        Path | None, typer.Option(help="Also write the comparison to this Markdown file.")
    ] = None,
) -> None:
    """Two runs of one dataset side by side, and the cases that changed."""

    async def both() -> tuple[
        tuple[RunRecord, list[evals_db.ResultRow]], tuple[RunRecord, list[evals_db.ResultRow]]
    ]:
        return await _run_and_rows(first), await _run_and_rows(second)

    (a, a_rows), (b, b_rows) = _in_loop(both)
    if a.dataset_id != b.dataset_id:
        raise _fail(
            f"{first} ran {a.dataset} and {second} ran {b.dataset}: only runs of the same "
            "dataset are comparable."
        )

    changes = report.compare(a_rows, b_rows)
    for line in [
        *report.describe(a),
        *report.describe(b),
        "",
        *report.headline([a, b]),
        "",
        *changes,
    ]:
        typer.echo(line)

    if markdown is not None:
        markdown.write_text(report.markdown([a, b], changes), encoding="utf-8")
        typer.secho(f"\nWritten to {markdown}", fg=DIM)
