"""``python -m portfolio_ai.ingestion``.

Thin on purpose. Everything this does is parse flags, run the pipeline, print a
summary and pick an exit code -- the last of which matters more than it sounds,
because this runs unattended at four in the morning and the exit code is the only
thing a scheduler can see.
"""

from typing import Annotated

import structlog
import typer

from portfolio_ai.cli import run_async
from portfolio_ai.ingestion.pipeline import IngestionReport, run
from portfolio_ai.llm.client import close_client

log = structlog.get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    help="Sync the knowledge base from GitHub into pgvector.",
)


def _print_summary(report: IngestionReport) -> None:
    """A human-readable summary on stdout, alongside the JSON log line.

    Two audiences. The structured log is for the analytics loop and for grepping a
    week of runs; this is for the person who just typed the command and wants to
    know what happened without reading JSON.
    """
    heading = "PLAN (nothing was written)" if report.dry_run else "INGESTION COMPLETE"
    typer.echo(f"\n{heading}")
    typer.echo(f"  discovered     {report.discovered}")
    typer.echo(f"  indexed        {report.indexed}")
    typer.echo(f"  unchanged      {report.unchanged}")
    typer.echo(f"  chunks         {report.chunks_written}")

    if report.rejected:
        typer.secho(f"  rejected       {len(report.rejected)}", fg=typer.colors.RED)
        # Each line says what happened to the article, not just to the file. A
        # rejected file whose previous version is still indexed is a stale article;
        # one that was never indexed is a missing one, and they need different
        # reactions.
        for rejection in report.rejected:
            typer.secho(f"                 {rejection.describe()}", fg=typer.colors.RED)

    if report.purged:
        typer.echo(f"  purged         {len(report.purged)}  ({', '.join(report.purged)})")

    label = "estimated" if report.dry_run else "actual"
    typer.echo(f"  tokens         {report.total_tokens:,} ({label})")
    typer.echo(f"  cost           ${report.cost:.6f} ({label})")
    typer.echo(f"  took           {report.duration_ms / 1000:.1f}s\n")


@app.command()
def main(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Plan only. No database writes and no OpenAI spend."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-fetch and re-embed everything, ignoring hashes."),
    ] = False,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Restrict to these doc_ids. Repeatable. Skips the purge step.",
            metavar="DOC_ID",
        ),
    ] = None,
) -> None:
    """Read the knowledge base from GitHub, embed what changed, store it."""
    # The report has to come back out of the coroutine, and run_async deliberately
    # returns an exit code rather than a value -- so it is collected here. A list
    # rather than a `nonlocal`, because it is only ever appended to once and this
    # reads more plainly than reassigning a closed-over name.
    completed: list[IngestionReport] = []

    async def _run() -> None:
        try:
            completed.append(await run(dry_run=dry_run, force=force, only=only or None))
        finally:
            # Closed whatever happened. Without it the process can exit while the
            # HTTP session is still open, which prints a warning that looks like a
            # bug in this code and is not.
            await close_client()

    # run_async owns the exit codes: 0 for a clean run, 1 for a deliberate refusal
    # (any PortfolioAIError -- a failed purge guard, bad configuration), 130 for
    # Ctrl-C. See lesson 5.
    code = run_async(_run)

    if completed:
        report = completed[0]
        _print_summary(report)

        # A run that skipped a document did not do what it was asked to. It is
        # still a partial success -- the other ten were indexed -- so this is a
        # non-zero exit rather than an exception, and the scheduler can notice
        # without the run being treated as a failure that rolled everything back.
        if report.rejected and code == 0:
            code = 1

    raise typer.Exit(code=code)


if __name__ == "__main__":  # pragma: no cover
    app()
