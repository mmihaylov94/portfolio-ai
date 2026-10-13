"""The weekly email: what it says, and what ``analytics digest`` does with it.

The command is what the crontab runs on a Monday, with nobody watching. So most of
this is about the cases where it must do nothing, quietly and correctly: mail not
set up, a week in which nobody asked anything, a dry run. And about the one promise
the email makes, that it carries the counts and none of what visitors typed.
"""

import dataclasses
import datetime as dt
import json
from decimal import Decimal

import click
import pytest
from smtp_fake import FakeServer, install_fake_smtp
from turns import START, turn
from typer.testing import CliRunner

from portfolio_ai.analytics import digest as digests
from portfolio_ai.analytics import report as reports
from portfolio_ai.analytics.cli import app
from portfolio_ai.analytics.report import Report
from portfolio_ai.config import get_settings
from portfolio_ai.db.analytics import ArticleUsage, ArticleUse, Turn, Volume
from portfolio_ai.exceptions import AnalyticsError

SENDER = "sender@example.com"
OWNER = "owner@example.com"
# Made up, in the shape Gmail gives an app password.
PASSWORD = "abcd efgh ijkl mnop"  # ruff: ignore[hardcoded-password-string]

UNTIL = START + dt.timedelta(days=1)

RATE = "What does Mihail charge for a day of consulting?"
LARAVEL = "Has Mihail shipped anything with Laravel?"
DECLINE = "His day rate is not listed in the information I have."
COMMENT = "That is not what I asked."


def _report(turns: list[Turn], *, days: int = 7) -> Report:
    return Report(
        since=UNTIL - dt.timedelta(days=days),
        until=UNTIL,
        turns=turns,
        previous=Volume(questions=4, conversations=2),
        pairs=[],
        refused=[],
        usage=ArticleUsage(
            searched=2,
            counted=2,
            articles=[
                ArticleUse(doc_id="services", title="Services", sections=6, unused=[], answers=2)
            ],
        ),
        low_score_threshold=0.35,
        rephrase_similarity=0.60,
        embedded=2,
        embedding_tokens=20,
        embedding_cost=Decimal("0.0000004"),
    )


def _week() -> Report:
    return _report(
        [
            turn(1, RATE, top_score=0.31, fallback_used=True, answer=DECLINE),
            turn(3, LARAVEL, conversation=2, rating=-1, comment=COMMENT),
        ]
    )


# --- composing ------------------------------------------------------------------


def test_the_subject_is_the_week_in_a_line() -> None:
    assert digests.compose(_week()).subject == (
        "mihaylov.io chat: 2 questions in the 7 days to 2026-10-13, 1 not answered"
    )


def test_one_question_in_one_day_is_not_plural() -> None:
    message = digests.compose(_report([turn(1, RATE)], days=1))

    assert (
        message.subject == "mihaylov.io chat: 1 question in the 1 day to 2026-10-13, 0 not answered"
    )


def test_the_body_is_the_counts_and_how_to_read_the_rest() -> None:
    body = digests.compose(_week()).body

    assert body.startswith("# Chat report: 2026-10-06 to 2026-10-13 (7 days)")
    assert "- 1 of 2 knowledge-base answers said the information was not there (50%)" in body
    assert "- 0 thumbs up, 1 thumbs down" in body
    assert "Services (services)" in body
    assert (
        "    docker compose exec worker python -m portfolio_ai.analytics report --since 8d\n"
        in body
    )


def test_the_command_in_the_email_looks_back_a_day_further_than_the_email_does() -> None:
    """`--since` counts back from when the command is run. An email built at 05:30 and
    read at nine would be followed by a report that starts three and a half hours
    after the email's own week did."""
    week = digests.compose(_week()).body
    month = digests.compose(_report([turn(1, RATE)], days=30)).body

    assert "report --since 8d" in week
    assert "report --since 31d" in month
    assert "a day more than this email covers" in week


def test_the_body_holds_nothing_a_visitor_typed_or_was_told() -> None:
    """The promise the email's own last paragraph makes. An email is kept for as long
    as its reader keeps it; the conversations it counts are deleted after 90 days."""
    report = _week()

    message = digests.compose(report)

    for item in report.turns:
        for text in (item.question, item.answer):
            assert text not in message.body
            assert text not in message.subject
    assert COMMENT not in message.body


def test_the_trial_message_is_not_a_report() -> None:
    message = digests.trial_message()

    assert message.subject == "mihaylov.io chat: test message"
    assert "digest --test" in message.body
    assert "Chat report" not in message.body


# --- the command ----------------------------------------------------------------


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install_fake_smtp(monkeypatch)


@pytest.fixture
def mail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_USER", SENDER)
    monkeypatch.setenv("SMTP_APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("DIGEST_TO_EMAIL", OWNER)
    get_settings.cache_clear()


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[dt.timedelta]:
    """Stand in for the database and OpenAI: a week with two questions in it. Records
    the length of each window asked for, so a test can tell whether it was built."""
    windows: list[dt.timedelta] = []

    async def build(*, since: dt.datetime, until: dt.datetime) -> Report:  # ruff: ignore[unused-async]
        windows.append(until - since)
        return _week()

    monkeypatch.setattr(reports, "build", build)
    return windows


@pytest.fixture
def quiet_week(monkeypatch: pytest.MonkeyPatch) -> None:
    async def build(*, since: dt.datetime, until: dt.datetime) -> Report:  # ruff: ignore[unused-async]
        # The window it was asked for, with nothing in it.
        return dataclasses.replace(_report([]), since=since, until=until)

    monkeypatch.setattr(reports, "build", build)


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["digest", *args])
    return result.exit_code, click.unstyle(result.output)


def _events(output: str) -> list[dict[str, object]]:
    """The JSON log lines in a command's output, which is all a scheduled run prints."""
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


@pytest.mark.usefixtures("mail_settings")
def test_a_week_with_questions_is_sent_once_and_logged_as_counts(
    server: FakeServer, built: list[dt.timedelta]
) -> None:
    code, output = _invoke()

    assert code == 0, output
    assert built == [dt.timedelta(days=7)], "seven days when --since is not given"

    [message] = server.sent
    assert message["Subject"] == digests.compose(_week()).subject
    assert message["From"] == SENDER
    assert message["To"] == OWNER
    assert message.get_content() == digests.compose(_week()).body

    [sent] = [event for event in _events(output) if event["event"] == "digest_sent"]
    assert (sent["questions"], sent["conversations"], sent["fallbacks"]) == (2, 2, 1)


@pytest.mark.usefixtures("mail_settings", "built")
def test_nothing_a_scheduled_run_prints_holds_a_question_or_an_address(
    server: FakeServer,
) -> None:
    """Under supercronic the output is the container's log. Not the questions, which
    the email leaves out too, and not who it was sent to: an address is personal data
    as much as a question is."""
    _, output = _invoke()

    assert server.sent, "it was sent; this is about what was printed while sending"
    for private in (RATE, LARAVEL, DECLINE, COMMENT, OWNER, SENDER, PASSWORD):
        assert private not in output


def test_without_mail_settings_it_warns_names_them_and_does_nothing_else(
    server: FakeServer, built: list[dt.timedelta]
) -> None:
    """Exit code 0, on purpose. The email is optional, and a job that failed every
    Monday for want of it would teach whoever reads the logs to skip past failures."""
    code, output = _invoke()

    assert code == 0
    [warning] = _events(output)
    assert warning["event"] == "digest_not_configured"
    assert warning["level"] == "warning"
    assert warning["missing"] == ["SMTP_USER", "SMTP_APP_PASSWORD", "DIGEST_TO_EMAIL"]
    assert built == [], "no report is built that could not be sent: nothing is spent"
    assert server.steps == []


@pytest.mark.usefixtures("mail_settings", "quiet_week")
def test_a_week_in_which_nobody_asked_anything_sends_nothing(server: FakeServer) -> None:
    code, output = _invoke()

    assert code == 0
    assert server.steps == [], "months without a visitor are months without an email"
    [skipped] = [event for event in _events(output) if event["event"] == "digest_skipped"]
    assert skipped["days"] == 7


@pytest.mark.usefixtures("mail_settings")
def test_since_sets_the_window(server: FakeServer, built: list[dt.timedelta]) -> None:
    code, _ = _invoke("--since", "30d")

    assert code == 0
    assert built == [dt.timedelta(days=30)]
    assert len(server.sent) == 1


@pytest.mark.usefixtures("mail_settings")
def test_a_since_that_is_not_days_fails_and_sends_nothing(
    server: FakeServer, built: list[dt.timedelta]
) -> None:
    code, output = _invoke("--since", "last week")

    assert code == 1
    assert "such as 7d or 30d" in output
    assert (built, server.steps) == ([], [])


@pytest.mark.parametrize("flags", [[], ["--test"], ["--dry-run"]], ids=["scheduled", "test", "dry"])
def test_a_wrong_since_fails_before_anything_else_is_looked_at(
    server: FakeServer, built: list[dt.timedelta], flags: list[str]
) -> None:
    """With no mail settings, on purpose. Checked after them, a mistyped --since in the
    crontab line would log "not configured" and exit 0 every Monday, and only start
    failing on the day the email was switched on."""
    code, output = _invoke("--since", "2w", *flags)

    assert code == 1
    assert "such as 7d or 30d" in output
    assert "digest_not_configured" not in output
    assert (built, server.steps) == ([], [])


@pytest.mark.usefixtures("built")
def test_a_dry_run_prints_the_email_and_needs_no_mail_settings(server: FakeServer) -> None:
    """Which is what makes it useful before the mailbox exists: this is how the email
    is read for the first time."""
    code, output = _invoke("--dry-run")

    assert code == 0, output
    message = digests.compose(_week())
    assert f"Subject: {message.subject}" in output
    assert "- 1 of 2 knowledge-base answers said the information was not there (50%)" in output
    assert "DRY RUN: nothing was sent." in output
    assert server.steps == []
    assert RATE not in output


@pytest.mark.usefixtures("mail_settings", "built")
def test_a_dry_run_sends_nothing_even_when_it_could(server: FakeServer) -> None:
    code, output = _invoke("--dry-run")

    assert code == 0
    assert "DRY RUN: nothing was sent." in output
    assert server.steps == []


@pytest.mark.usefixtures("quiet_week")
def test_a_dry_run_of_a_quiet_week_says_there_is_nothing_to_send(server: FakeServer) -> None:
    code, output = _invoke("--dry-run")

    assert code == 0
    assert "Nothing to send: nobody asked anything in the last 7 day(s)." in output
    assert "Subject:" not in output
    assert server.steps == []


@pytest.mark.usefixtures("mail_settings", "built")
def test_a_send_that_fails_is_exit_code_one_and_one_line(server: FakeServer) -> None:
    """Nothing is lost: the questions are still in the database, and the command is
    run again. The scheduler needs the exit code and the log needs the reason."""
    server.fail_connect = ConnectionRefusedError(111, "Connection refused")

    code, output = _invoke()

    assert code == 1
    [failed] = [event for event in _events(output) if event["event"] == "command_failed"]
    assert failed["error"] == "The email could not be sent (ConnectionRefusedError)."
    assert failed["error_type"] == "MailError"
    assert not [event for event in _events(output) if event["event"] == "digest_sent"]
    assert "Traceback" not in output


@pytest.mark.usefixtures("mail_settings")
def test_a_report_that_could_not_be_built_sends_nothing(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def build(*, since: dt.datetime, until: dt.datetime) -> Report:  # ruff: ignore[unused-async]
        raise AnalyticsError(f"nothing for {since:%Y} to {until:%Y}")

    monkeypatch.setattr(reports, "build", build)

    code, _ = _invoke()

    assert code == 1
    assert server.steps == []


# --- digest --test ----------------------------------------------------------------


@pytest.mark.usefixtures("mail_settings")
def test_the_test_flag_sends_one_line_and_builds_no_report(
    server: FakeServer, built: list[dt.timedelta]
) -> None:
    code, output = _invoke("--test")

    assert code == 0, output
    assert built == [], "no database, no OpenAI: this proves the mail settings and nothing else"
    [message] = server.sent
    assert message["Subject"] == "mihaylov.io chat: test message"
    assert message["To"] == OWNER
    [sent] = [event for event in _events(output) if event["event"] == "digest_sent"]
    assert sent["test"] is True


def test_the_test_flag_without_mail_settings_fails_and_names_what_is_missing(
    server: FakeServer,
) -> None:
    """Unlike the scheduled run, which shrugs. Somebody asked for a test: "not
    configured" is the answer, and it is a failure."""
    code, output = _invoke("--test")

    assert code == 1
    assert "Cannot send a test. Not set: SMTP_USER, SMTP_APP_PASSWORD, DIGEST_TO_EMAIL." in output
    assert server.steps == []
