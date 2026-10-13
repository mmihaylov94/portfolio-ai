"""``python -m portfolio_ai.analytics`` -- the analytics jobs.

::

    purge [--dry-run]            delete chat older than CHAT_RETENTION_DAYS; nightly, by cron
    digest [--since 7d]          email the week's counts; Mondays, by cron
           [--dry-run | --test]  print the email and send nothing | send a one-line test
    report [--since 7d]          what was asked and where answers fell short; for a terminal
    seed [--clear] [--dry-run]   invented conversations, into the development database only

``report`` quotes what visitors typed. It is for a person at a terminal and must
never be put in the crontab: under supercronic a command's output is the
container's log, and nothing a visitor types goes into a log. ``digest`` is the
one that is scheduled, and it carries the counts without the words.

The content gaps and ``promote-case`` are the rest of build step 7.
"""

import io
import sys
from pathlib import Path
from typing import Annotated

import structlog
import typer

from portfolio_ai.analytics import digest as digests
from portfolio_ai.analytics import mail, retention, seed
from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.retention import RetentionReport
from portfolio_ai.analytics.seed import InventedTraffic, Played, SeedReport
from portfolio_ai.cli import run_async
from portfolio_ai.db.pool import close_pool
from portfolio_ai.exceptions import ConfigError, PortfolioAIError
from portfolio_ai.llm.client import close_client

log = structlog.get_logger(__name__)

app = typer.Typer(add_completion=False, help="Analytics over the chat history.")


@app.callback()
def _commands() -> None:
    """Analytics over the chat history.

    A Typer app with exactly one command and no callback runs that command
    directly, so ``python -m portfolio_ai.analytics purge`` would fail with
    "unexpected extra argument (purge)". That was the first reason for this
    callback, when ``purge`` was the only command, and it is why it must stay if
    the commands are ever cut back to one.
    """
    # On Windows, output piped or redirected to a file is encoded in the ANSI code
    # page, and Python raises UnicodeEncodeError for any character outside it. A
    # visitor's question or a model's answer supplies those freely. "replace" prints
    # "?" for them instead of stopping the report halfway. isinstance, because
    # sys.stdout is typed as TextIO, which has no reconfigure(), and may be a StringIO.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")


async def _shutdown() -> None:
    await close_client()
    await close_pool()


# --- purge ----------------------------------------------------------------------


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


# --- report ---------------------------------------------------------------------


@app.command("report")
def show_report(
    since: Annotated[
        str, typer.Option("--since", help="How far back to look, in days: 7d, 30d.")
    ] = "7d",
) -> None:
    """What was asked, and where the answers fell short. Quotes visitors: terminal only."""
    built: list[reports.Report] = []

    async def _run() -> None:
        try:
            start, end = reports.window(since)
            built.append(await reports.build(since=start, until=end))
        finally:
            await _shutdown()

    # Warnings only. At the usual level the report would arrive after a line for
    # the pool opening and another for the embeddings call.
    code = run_async(_run, log_level="WARNING")
    if built:
        typer.echo(reports.render(built[0], verbatim=True))
    raise typer.Exit(code=code)


# --- digest ---------------------------------------------------------------------


@app.command("digest")
def send_digest(
    since: Annotated[
        str, typer.Option("--since", help="How far back to look, in days: 7d, 30d.")
    ] = "7d",
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the email, and send nothing.")
    ] = False,
    test: Annotated[
        bool, typer.Option("--test", help="Send a one-line test, to prove the mail settings.")
    ] = False,
) -> None:
    """Email the window's counts. Nothing a visitor typed is in it."""
    outbox: list[digests.Message] = []
    counts: dict[str, int] = {}
    quiet: list[int] = []

    async def _run() -> None:
        try:
            # First, whatever else is true. A wrong --since in the crontab line should
            # fail on its first Monday, and not pass quietly every week until the
            # mail settings arrive.
            start, end = reports.window(since)

            missing = mail.missing_settings()
            if test:
                if missing:
                    raise ConfigError(f"Cannot send a test. Not set: {', '.join(missing)}.")
                outbox.append(digests.trial_message())
                return
            if missing and not dry_run:
                # Not an error. The email is optional, and a job that failed every
                # Monday for want of it would teach whoever reads the logs to skip
                # past failures. Said once a week, at a level that is hard to miss.
                log.warning("digest_not_configured", missing=missing)
                return

            report = await reports.build(since=start, until=end)
            if not report.turns:
                # Months without a visitor are months without an email.
                log.info("digest_skipped", reason="nobody asked anything", days=report.days)
                quiet.append(report.days)
                return
            outbox.append(digests.compose(report))
            counts.update(
                questions=len(report.turns),
                conversations=report.conversations,
                fallbacks=len(report.fallbacks),
            )
        finally:
            await _shutdown()

    # A person reading a dry run wants the email, not the lines about the pool. The
    # scheduled run keeps the settings' level: its log lines are its whole output.
    code = run_async(_run, log_level="WARNING" if dry_run else None)

    if code == 0 and dry_run:
        if quiet:
            typer.echo(f"Nothing to send: nobody asked anything in the last {quiet[0]} day(s).")
        for message in outbox:
            typer.echo(f"Subject: {message.subject}\n\n{message.body}")
            typer.echo("DRY RUN: nothing was sent.")
    elif code == 0 and outbox:
        box = mail.mailbox()
        if box is None:  # pragma: no cover - missing_settings() said otherwise above
            raise RuntimeError("the mail settings went missing between two reads")
        # After the event loop has finished, on purpose: smtplib blocks, and there
        # is now nothing for it to block (see analytics/mail.py).
        try:
            mail.send(box, subject=outbox[0].subject, body=outbox[0].body)
        except PortfolioAIError as exc:
            # The same line run_async writes for a refusal, since this happens after
            # it has returned. The message is written to hold no address.
            log.error("command_failed", error=str(exc), error_type=type(exc).__name__)
            code = 1
        else:
            # Counts, as always. Not the recipient: an address is personal data too.
            log.info("digest_sent", test=test, **counts)

    raise typer.Exit(code=code)


# --- seed -----------------------------------------------------------------------


def _print_plan(traffic: InventedTraffic, already: int) -> None:
    messages = traffic.messages
    typer.echo(f"\nPLAN (nothing was asked, spent or stored): {traffic.description.strip()}")
    typer.echo(f"  conversations  {len(traffic.conversations)}")
    typer.echo(f"  questions      {len(messages)}")
    for kind in ("answerable", "gap", "small_talk", "off_topic"):
        typer.echo(f"    {kind:<13}{sum(message.kind == kind for message in messages)}")
    typer.echo(f"  asked twice    {sum(message.rephrases_previous for message in messages)}")
    typer.echo(f"  thumbs         {sum(message.rating is not None for message in messages)}")
    typer.echo(f"  about          ${len(messages) * seed.ESTIMATED_COST_PER_QUESTION:.2f}")
    if already:
        typer.secho(
            f"  {already} invented conversation(s) are stored already: "
            "`seed --clear` before seeding again.",
            fg="red",
        )
    typer.echo("")


def _print_played(item: Played) -> None:
    result = item.result
    score = f"{result.top_score:.2f}" if result.top_score is not None else "  - "
    fallback = "  fallback" if result.fallback_used else ""
    typer.echo(
        f"  {item.conversation:<24}{item.message.kind:<12}{result.classification:<16}"
        f"{score}{fallback}"
    )


def _print_seeded(report: SeedReport) -> None:
    typer.echo("\nSEEDED")
    typer.echo(f"  conversations  {report.conversations}")
    typer.echo(f"  questions      {len(report.played)}")
    typer.echo(f"  cost           ${report.cost:.4f}\n")
    typer.echo(f"  {'meant to be':<14}{'routed as':<18}{'questions':>9}")
    for (kind, route), count in sorted(report.routes.items()):
        typer.echo(f"  {kind:<14}{route:<18}{count:>9}")
    typer.echo("")
    for kind, count in sorted(report.fallbacks.items()):
        typer.echo(f"  {count} {kind} question(s) got the fallback answer")
    typer.echo("\nNext: python -m portfolio_ai.analytics report --since 90d\n")


@app.command("seed")
def seed_invented(
    path: Annotated[
        Path, typer.Argument(help="The file of invented conversations.")
    ] = seed.DEFAULT_PATH,
    clear: Annotated[
        bool, typer.Option("--clear", help="Delete the invented conversations instead.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Check the file and print the plan. Spends nothing.")
    ] = False,
) -> None:
    """Play invented conversations through the assistant. Development database only."""
    planned: list[tuple[InventedTraffic, int]] = []
    seeded: list[SeedReport] = []
    removed: list[int] = []

    async def _run() -> None:
        try:
            if clear:
                removed.append(await seed.stored() if dry_run else await seed.clear())
                return
            # Before the file is read. The image has no datasets/ folder, so on the
            # server a missing file would be reported first, and the answer there
            # has to be that this does not run in production at all.
            already = await seed.stored()
            traffic = seed.load(path)
            if dry_run:
                planned.append((traffic, already))
                return
            seeded.append(await seed.seed(traffic, on_played=_print_played))
        finally:
            await _shutdown()

    code = run_async(_run, log_level="WARNING")
    if removed:
        verb = "would be removed" if dry_run else "removed"
        typer.echo(f"{removed[0]} invented conversation(s) {verb}.")
    if planned:
        _print_plan(*planned[0])
    if seeded:
        _print_seeded(seeded[0])
    raise typer.Exit(code=code)
