"""``python -m portfolio_ai.contact try``: the flow by hand, with nothing sent.

The command runs the real flow up to the sending, so OpenAI is the fake here and the
mail stand-in is installed only to show that it was never touched.
"""

import click
import pytest
from openai_fake import FakeOpenAI, install_fake_openai
from smtp_fake import FakeServer, install_fake_smtp
from typer.testing import CliRunner

from portfolio_ai.contact import emails
from portfolio_ai.contact.cli import app

MESSAGE = "We track stock in spreadsheets and would like a small web tool for our team."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


@pytest.fixture(autouse=True)
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install_fake_smtp(monkeypatch)


def _invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["try", *args])
    return result.exit_code, click.unstyle(result.output)


def test_try_prints_what_would_be_sent_and_sends_nothing(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    fake.verdict(explanation="A clear request for an internal tool.")
    fake.paragraphs("Thank you for getting in touch.", "How many people would use it?")

    code, output = _invoke("--message", MESSAGE, "--subject", "A warehouse tool")

    assert code == 0, output
    assert "status       accepted" in output
    assert "classifier   project (0.9), spam 0.05, auto-reply yes" in output
    assert "A clear request for an internal tool." in output
    visitor = output.index("--- to the visitor")
    owner = output.index("--- to the owner")
    assert visitor < owner
    assert (
        "Subject: Your message to Mihail Mihaylov\n\nHello,\n\nThank you for getting in touch."
        in output
    )
    # The command keeps no count, and says so.
    assert "limits       one reply a day per address, and a daily cap: not applied here" in output
    assert emails.AUTOMATIC in output[visitor:owner]
    assert (
        "Subject: mihaylov.io - VALID Contact Form Submission [AUTO-REPLIED] - A warehouse tool"
        in output[owner:]
    )
    assert output.rstrip().endswith("NOTHING WAS SENT.")
    assert server.steps == [], "not even a connection"
    # No mail settings exist in a unit test. The command needs none: it never sends.


def test_try_says_why_there_would_be_no_acknowledgement(fake: FakeOpenAI) -> None:
    fake.verdict(should_auto_reply=False)

    code, output = _invoke("--message", MESSAGE)

    assert code == 0, output
    assert "No acknowledgement: The classifier advised against an automatic reply." in output
    assert "--- to the visitor" not in output
    assert "[NO AUTO-REPLY]" in output


def test_try_shows_the_rules_hits_and_asks_no_model_for_what_they_block(
    fake: FakeOpenAI,
) -> None:
    code, output = _invoke("--message", "seo??", "--email", "someone@mailinator.com")

    assert code == 0, output
    assert fake.requests == []
    assert "status       rule_spam" in output
    assert "spam score   6" in output
    assert "             - Disposable email domain" in output
    assert "[RULE_SPAM]" in output
    assert "cost         $0.000000" in output


def test_try_marks_a_suspicious_message(fake: FakeOpenAI) -> None:
    fake.verdict()

    _, output = _invoke("--message", "seo??")

    assert "spam score   4  (suspicious)" in output
    assert "No acknowledgement: The fixed rules found the message suspicious." in output


def test_try_still_shows_the_owners_email_when_openai_cannot_be_reached(
    fake: FakeOpenAI,
) -> None:
    fake.http_error(500)

    code, output = _invoke("--message", MESSAGE)

    assert code == 0, output
    assert "status       not_classified" in output
    assert "[NOT CLASSIFIED]" in output


def test_try_needs_a_message() -> None:
    code, output = _invoke("--reason", "job")

    assert code == 2
    assert "--message" in output


def test_the_commands_name_has_to_be_typed() -> None:
    """One command and a callback: without the callback Typer would run ``try``
    directly, and ``python -m portfolio_ai.contact try`` would be refused for an
    unexpected argument."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "try" in click.unstyle(result.output)
