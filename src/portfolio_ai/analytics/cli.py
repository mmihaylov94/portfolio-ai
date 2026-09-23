"""``python -m portfolio_ai.analytics`` -- the analytics jobs. For now, only ``purge``.

The weekly digest, the content-gap clustering and ``promote-case`` land with build
step 7, once there is traffic worth analysing. The retention sweep could not wait
for them: it has to be running before the first visitor's message is stored.
"""

from typing import Annotated

import typer

from portfolio_ai.analytics import retention
from portfolio_ai.analytics.retention import RetentionReport
from portfolio_ai.cli import run_async
from portfolio_ai.db.pool import close_pool

app = typer.Typer(add_completion=False, help="Analytics over the chat history.")


@app.callback()
def _commands() -> None:
    """Analytics over the chat history.

    This callback does nothing, and removing it would break the crontab. A Typer app
    with exactly one command and no callback runs that command directly, so
    ``python -m portfolio_ai.analytics purge`` would fail with "unexpected extra
    argument (purge)". The callback makes this a group of commands, which is what it
    will be once the digest lands.
    """


def _print_summary(report: RetentionReport) -> None:
    if report.cutoff is None:
        typer.secho("\nRETENTION IS OFF (CHAT_RETENTION_DAYS=0): nothing was deleted.\n", fg="red")
        return

    heading = "PLAN (nothing was deleted)" if report.dry_run else "PURGE COMPLETE"
    typer.echo(f"\n{heading}")
    typer.echo(f"  kept for       {report.days} days")
    typer.echo(f"  cutoff         {report.cutoff:%Y-%m-%d %H:%M} UTC")
    typer.echo(f"  messages       {report.messages}")
    typer.echo(f"  sessions       {report.sessions}\n")


@app.command()
def purge(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count what would be deleted, and delete nothing."),
    ] = False,
) -> None:
    """Delete chat messages and sessions older than CHAT_RETENTION_DAYS."""
    # Same shape as the ingestion CLI: run_async owns logging, the event loop and
    # the exit codes, and the report comes back out through a list.
    completed: list[RetentionReport] = []

    async def _run() -> None:
        try:
            completed.append(await retention.purge(dry_run=dry_run))
        finally:
            await close_pool()

    code = run_async(_run)
    if completed:
        _print_summary(completed[0])
    raise typer.Exit(code=code)
