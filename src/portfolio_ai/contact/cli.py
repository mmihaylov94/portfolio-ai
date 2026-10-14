"""``python -m portfolio_ai.contact`` -- try the contact form's flow by hand.

::

    try --reason project --email you@example.com --subject "..." --message "..."

``try`` puts one made-up submission through the rules and the two models and prints
what would be sent: the acknowledgement to the visitor and the notification to the
owner. **It sends nothing.** It is how a change to a prompt is read before it goes
live, and it costs what two small model calls cost, a fraction of a cent.

There is no command that sends. The form on the site is the way to do that: the
real path, end to end, with the real mailbox at the other end.
"""

from typing import Annotated

import typer

from portfolio_ai.cli import run_async
from portfolio_ai.contact import service
from portfolio_ai.contact.rules import REASONS
from portfolio_ai.contact.service import Prepared
from portfolio_ai.llm.client import close_client

app = typer.Typer(add_completion=False, help="Try the contact form's flow by hand.")


@app.callback()
def _commands() -> None:
    """Try the contact form's flow by hand.

    A Typer app with exactly one command and no callback runs that command directly,
    so ``python -m portfolio_ai.contact try`` would fail with "unexpected extra
    argument (try)". The callback is what makes ``try`` a name that has to be typed.
    """


def _print(prepared: Prepared) -> None:
    checked, verdict = prepared.checked, prepared.verdict
    typer.echo(f"\nstatus       {prepared.status}")
    typer.echo(
        f"spam score   {checked.spam_score}" + ("  (suspicious)" if checked.suspicious else "")
    )
    for hit in checked.rule_hits:
        typer.echo(f"             - {hit}")
    if verdict is not None:
        typer.echo(
            f"classifier   {verdict.detected_reason} ({verdict.reason_confidence:g}), "
            f"spam {verdict.spam_confidence:g}, "
            f"auto-reply {'yes' if verdict.should_auto_reply else 'no'}"
        )
        typer.echo(f"             {verdict.explanation}")
    typer.echo(f"cost         ${prepared.cost:.6f}")

    acknowledgement = prepared.acknowledgement
    if acknowledgement is not None:
        # The API also allows one reply per address a day, and so many in all. This
        # command keeps no count, so it shows the reply as if both had let it through.
        typer.echo("limits       one reply a day per address, and a daily cap: not applied here")
        typer.echo("\n--- to the visitor " + "-" * 60)
        typer.echo(f"Subject: {acknowledgement.subject}\n")
        typer.echo(acknowledgement.text)
    elif prepared.no_reply:
        typer.echo(f"\nNo acknowledgement: {prepared.no_reply}")

    # As if the acknowledgement had gone: this prints what would be sent, and
    # sends neither.
    note = service.notification(prepared, sent=acknowledgement is not None)
    typer.echo("\n--- to the owner " + "-" * 62)
    typer.echo(f"Subject: {note.subject}\n")
    typer.echo(note.text)
    typer.echo("NOTHING WAS SENT.")


@app.command("try")
def try_one(
    message: Annotated[str, typer.Option("--message", help="What the visitor wrote.")],
    subject: Annotated[str, typer.Option("--subject", help="The subject line.")] = "A question",
    email: Annotated[
        str, typer.Option("--email", help="The visitor's address.")
    ] = "visitor@example.com",
    reason: Annotated[
        str, typer.Option("--reason", help=f"One of: {', '.join(sorted(REASONS))}.")
    ] = "project",
) -> None:
    """Put one made-up submission through the flow and print the emails. Sends nothing."""
    made: list[Prepared] = []

    async def _run() -> None:
        try:
            made.append(
                await service.prepare(email=email, subject=subject, message=message, reason=reason)
            )
        finally:
            await close_client()

    # Warnings only, so that what is printed is the emails and not a line per call.
    code = run_async(_run, log_level="WARNING")
    for prepared in made:
        _print(prepared)
    raise typer.Exit(code=code)
