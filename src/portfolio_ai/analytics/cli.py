"""``python -m portfolio_ai.analytics`` -- the analytics jobs.

::

    purge [--dry-run]            delete chat older than CHAT_RETENTION_DAYS; nightly, by cron
    digest [--since 7d]          email the week's counts; Mondays, by cron
           [--dry-run | --test]  print the email and send nothing | send a one-line test
    report [--since 7d]          what was asked and where answers fell short; for a terminal
    refresh-gaps [--dry-run]     work the content gaps out again; prints no question
    gaps [--status open]         the writing queue, with example questions; for a terminal
    gap-status 7 written --doc faq    mark a gap written, wont_fix or open again
    promote-case 812 --expected-doc faq    a visitor's question as an eval case, printed
    seed [--clear] [--dry-run]   invented conversations, into the development database only

``report``, ``gaps`` and ``promote-case`` quote what visitors typed. They are for a
person at a terminal and must never be put in the crontab: under supercronic a
command's output is the container's log, and nothing a visitor types goes into a
log. ``digest`` is the one that is scheduled; it refreshes the gaps and carries the
counts and the gaps' names, without the words.
"""

import io
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import structlog
import typer

from portfolio_ai.analytics import digest as digests
from portfolio_ai.analytics import gaps as gap_refresh
from portfolio_ai.analytics import mail, promote, retention, seed
from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.retention import RetentionReport
from portfolio_ai.analytics.seed import InventedTraffic, Played, SeedReport
from portfolio_ai.cli import run_async
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db.pool import close_pool
from portfolio_ai.exceptions import AnalyticsError, ConfigError, PortfolioAIError
from portfolio_ai.llm.client import close_client

log = structlog.get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    # Typer prints a crash with rich, which can list every local variable of every
    # frame beside the traceback. The frames here hold visitors' questions, and a
    # scheduled command's output is the container's log. Off is the default in the
    # version this was written against; saying so here keeps it off in the next.
    pretty_exceptions_show_locals=False,
    help="Analytics over the chat history.",
)


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
    """Email the window's counts and the content gaps' names. Nothing a visitor typed is in it."""
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

            if not dry_run:
                # The gaps are worked out again before the report reads them, so that
                # the email names them as they stand. In a week nobody asked anything
                # too: the purge goes on taking questions, and a gap's count should
                # follow them down. Not in a dry run: a refresh writes to the table
                # and pays a model to name what is new, and a dry run does neither.
                # It shows the gaps as they stood.
                await gap_refresh.refresh()
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


# --- content gaps ---------------------------------------------------------------


@app.command("refresh-gaps")
def refresh_gaps(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Group and match, name nothing, write nothing."),
    ] = False,
) -> None:
    """Work the content gaps out again from the questions that failed."""
    done: list[gap_refresh.Refresh] = []

    async def _run() -> None:
        try:
            done.append(await gap_refresh.refresh(dry_run=dry_run))
        finally:
            await _shutdown()

    code = run_async(_run, log_level="WARNING")
    for result in done:
        heading = "PLAN (nothing was named or written)" if result.dry_run else "GAPS REFRESHED"
        typer.echo(f"\n{heading}")
        typer.echo(f"  failed questions  {result.failed}")
        if result.capped:
            typer.echo(
                f"                    more are stored: the newest {gap_refresh.MAX_QUESTIONS} "
                "were grouped"
            )
        typer.echo(f"  gaps              {len(result.groups)}")
        typer.echo(f"    new             {len(result.created)}")
        typer.echo(f"    known           {len(result.updated)}")
        if result.unnamed and not result.dry_run:
            typer.echo(f"    not named yet   {result.unnamed}: the next refresh asks again")
        typer.echo(f"  {'would empty' if result.dry_run else 'emptied':<18}{result.emptied}")
        typer.echo(f"  cost              ${result.cost:.6f}\n")
        # Labels and counts. Never a question: this is also what `digest` does on a
        # Monday, and its output there would be the container's log.
        for group in result.groups:
            known = "known" if group.gap_id is not None else "new  "
            typer.echo(f"  {len(group.questions):>4}  {known}  {group.label or '(not named yet)'}")
        typer.echo("\nThe questions behind each: python -m portfolio_ai.analytics gaps\n")
    raise typer.Exit(code=code)


class GapFilter(StrEnum):
    OPEN = "open"
    WRITTEN = "written"
    WONT_FIX = "wont_fix"
    ALL = "all"


@app.command("gaps")
def show_gaps(
    status: Annotated[
        GapFilter, typer.Option("--status", help="Which gaps to list.")
    ] = GapFilter.OPEN,
) -> None:
    """The writing queue, most asked first. Quotes visitors: terminal only."""
    # Each gap with up to three of its example questions, as (id, text): the id is
    # what `promote-case` takes once the article is written.
    found: list[tuple[analytics_db.Gap, list[tuple[int, str]]]] = []

    async def _run() -> None:
        try:
            wanted = None if status is GapFilter.ALL else status.value
            listed = await analytics_db.list_gaps(status=wanted)
            # The examples are ids. Only the questions still stored come back; the
            # rest went with the purge, which is the point of keeping ids. One query
            # for every gap's examples together, and not one for each gap.
            kept = await analytics_db.questions_by_id(
                [i for gap in listed for i in gap.example_message_ids]
            )
            for gap in listed:
                found.append(
                    (gap, [(i, kept[i]) for i in gap.example_message_ids if i in kept][:3])
                )
        finally:
            await _shutdown()

    code = run_async(_run, log_level="WARNING")
    if code == 0 and not found:
        typer.echo("No gaps to list. `refresh-gaps` works them out from the questions that failed.")
    for gap, examples in found:
        score = f"{gap.avg_top_score:.3f}" if gap.avg_top_score is not None else "  -- "
        typer.echo(
            f"\n#{gap.id}  {gap.label}  [{gap.status}]\n"
            f"    {gap.question_count} question(s), {gap.fallback_count} said the information "
            f"was not there, average best score {score}\n"
            f"    first asked {gap.first_seen_at:%Y-%m-%d}, last {gap.last_seen_at:%Y-%m-%d}"
        )
        if gap.resolved_at is not None:
            closed_by = f", closed by {gap.resolved_doc_id}" if gap.resolved_doc_id else ""
            typer.echo(f"    marked {gap.status} on {gap.resolved_at:%Y-%m-%d}{closed_by}")
        # For a gap marked written, and no other: there an article was meant to have
        # closed it. A gap decided against is expected to go on being asked.
        if gap.status == "written" and gap.failed_since_resolved:
            typer.echo("    asked again, and unanswered, since then")
        for question_id, example in examples:
            typer.echo(f"      - #{question_id}  {' '.join(example.split())[:160]}")
    raise typer.Exit(code=code)


class GapStatus(StrEnum):
    OPEN = "open"
    WRITTEN = "written"
    WONT_FIX = "wont_fix"


@app.command("gap-status")
def set_gap_status(
    gap_id: Annotated[int, typer.Argument(help="The gap's number, as `gaps` prints it.")],
    status: Annotated[GapStatus, typer.Argument(help="open, written or wont_fix.")],
    doc: Annotated[
        str | None,
        typer.Option("--doc", help="The doc_id of the article that closes it. With `written`."),
    ] = None,
) -> None:
    """Mark a gap written, wont_fix, or open again."""

    async def _run() -> None:
        try:
            if status is GapStatus.WRITTEN:
                if doc is None:
                    raise AnalyticsError(
                        "`written` needs --doc: the doc_id of the article that closes the gap."
                    )
                if not await analytics_db.document_exists(doc):
                    raise AnalyticsError(
                        f"No indexed article has the doc_id {doc!r}. Write it, let the hourly "
                        "ingest pick it up, and mark the gap then."
                    )
            elif doc is not None:
                raise AnalyticsError("--doc goes with `written` only.")

            if not await analytics_db.set_gap_status(gap_id, status.value, doc_id=doc):
                raise AnalyticsError(f"There is no gap {gap_id}. `gaps --status all` lists them.")
        finally:
            await _shutdown()

    code = run_async(_run, log_level="WARNING")
    if code == 0:
        typer.echo(f"Gap {gap_id} is now {status.value}" + (f", closed by {doc}." if doc else "."))
    raise typer.Exit(code=code)


# --- promote-case ---------------------------------------------------------------


def _utf8_output() -> None:
    """Write this command's output as UTF-8 bytes, whatever the console's encoding.

    What `promote-case` prints is on its way into a YAML file in the repository,
    which is UTF-8. On Windows, output redirected to a file is otherwise written in
    the ANSI code page, and the callback above then prints "?" for every character
    that page lacks. In a report that is a blemish. In a case about to be committed
    it is a question quietly changed, and one saved in the wrong encoding besides.
    An interactive console and a Linux container are UTF-8 already, so there this
    changes nothing.

    This decides the bytes Python writes, and no more. Command Prompt's `>` and
    bash's put those bytes in the file as they are. Windows PowerShell's `>` and `|`
    do not: it decodes a program's output with the console's own code page and
    writes the result out again as UTF-16, so the file holds other characters, and
    nothing here can prevent it. In PowerShell, copy the case from the terminal.

    `errors` is given again because `reconfigure(encoding=...)` puts it back to
    "strict" when it is left out, which would undo the callback's setting.
    """
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@app.command("promote-case")
def promote_case(
    message_id: Annotated[
        int, typer.Argument(help="A question's number, as `report` and `gaps` print it.")
    ],
    expected_doc: Annotated[
        list[str],
        typer.Option(
            "--expected-doc", help="The doc_id of an article that answers it. Repeatable."
        ),
    ],
    key: Annotated[
        str | None,
        typer.Option("--key", help="A name for the case: lower-case-with-hyphens."),
    ] = None,
    with_history: Annotated[
        bool,
        typer.Option("--with-history", help="Include the conversation before the question."),
    ] = False,
) -> None:
    """Print a visitor's question as an eval case. Quotes visitors: terminal only."""
    # A list with no default is how Typer is told an option is required: it refuses
    # the command, with a usage error, before any of this runs.
    printed: list[str] = []

    async def _run() -> None:
        try:
            case = await promote.promote(
                message_id, expected_docs=expected_doc, key=key, with_history=with_history
            )
            printed.extend([promote.as_yaml(case), promote.what_to_do(case)])
        finally:
            await _shutdown()

    code = run_async(_run, log_level="WARNING")
    _utf8_output()
    for text in printed:
        typer.echo(text)
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
    counted: list[int] = []
    removed: list[seed.Cleared] = []

    async def _run() -> None:
        try:
            if clear:
                if dry_run:
                    counted.append(await seed.stored())
                else:
                    removed.append(await seed.clear())
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
    for count in counted:
        typer.echo(f"{count} invented conversation(s) would be removed, and every content gap.")
    for cleared in removed:
        typer.echo(
            f"{cleared.conversations} invented conversation(s) and "
            f"{cleared.gaps} content gap(s) removed."
        )
    if planned:
        _print_plan(*planned[0])
    if seeded:
        _print_seeded(seeded[0])
    raise typer.Exit(code=code)
