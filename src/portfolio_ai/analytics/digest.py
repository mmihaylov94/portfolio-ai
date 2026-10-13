"""The weekly email: a report's counts, composed as a message.

Nothing here reads a database or sends anything. ``report.build`` makes the report,
``mail.send`` sends, and this module is the step between: a subject line and a body
from a :class:`~portfolio_ai.analytics.report.Report`.

**The body holds nothing a visitor typed.** It is ``report.render`` with
``verbatim=False``, the form with counts, article titles, costs and timings. The
chat tells visitors their conversations are kept for 90 days, and an email outlives
that for as long as its reader keeps it. So the questions themselves stay in the
database, and the email's last line says how to read them there.
"""

from dataclasses import dataclass

from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.report import Report

# What the subject starts with, so that a mail filter has something fixed to match.
SUBJECT_PREFIX = "mihaylov.io chat"

# The command that prints the questions, as it is run on the server. `exec`, so that
# the output goes to the terminal that asked and not to the container's log.
HOW_TO_READ_THEM = "docker compose exec worker python -m portfolio_ai.analytics report"


@dataclass(frozen=True)
class Message:
    subject: str
    body: str


def compose(report: Report) -> Message:
    """The week as an email. Call it only for a window somebody asked something in:
    an empty week sends nothing, and that decision is the command's."""
    days = report.days
    asked = len(report.turns)
    declined = len(report.fallbacks)

    # The whole message in a line, for an inbox that shows only subjects: how much
    # was asked, and how much of it the knowledge base could not answer.
    subject = (
        f"{SUBJECT_PREFIX}: {asked} question{'' if asked == 1 else 's'} in the "
        f"{days} day{'' if days == 1 else 's'} to {report.until:%Y-%m-%d}, "
        f"{declined} not answered"
    )
    body = "\n\n".join(
        [
            reports.render(report, verbatim=False),
            "---",
            "This email holds counts and none of what visitors typed. To read the "
            "questions behind them, on the server:\n\n"
            # A day more than the email covers. `--since` counts back from the moment
            # the command is run, which is some hours after this was sent; the same
            # number of days would start after this email's first questions.
            f"    {HOW_TO_READ_THEM} --since {days + 1}d\n\n"
            "That is a day more than this email covers, so that it still reaches the "
            "start of the week when it is run later in the day.",
        ]
    )
    return Message(subject=subject, body=body + "\n")


def trial_message() -> Message:
    """What ``digest --test`` sends: enough to prove the mail settings, and no report.

    Not called ``test_message``: pytest collects any function whose name starts with
    ``test_`` from a test module, an imported one included, and would run this as a
    test of nothing.
    """
    return Message(
        subject=f"{SUBJECT_PREFIX}: test message",
        body=(
            "This is a test of the weekly digest's mail settings, sent by "
            "`analytics digest --test`. If you are reading it, they work.\n"
        ),
    )
