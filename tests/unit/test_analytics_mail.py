"""Sending an email, with the mail server replaced by something that records.

What matters here is the order of the conversation (encrypt, then log in, then
send), that the certificate is really checked, and what a failure says: enough to
fix it, and never the password or an address, because the message is logged.
"""

import smtplib
import socket
import ssl
from email.utils import parsedate_to_datetime

import pytest
from pydantic import SecretStr
from smtp_fake import FakeServer, install_fake_smtp

from portfolio_ai.analytics import mail
from portfolio_ai.analytics.mail import Mailbox
from portfolio_ai.config import get_settings
from portfolio_ai.exceptions import ConfigError, MailError

SENDER = "sender@example.com"
OWNER = "owner@example.com"
# Made up, in the shape Gmail gives an app password: sixteen letters in four groups.
PASSWORD = "abcd efgh ijkl mnop"  # ruff: ignore[hardcoded-password-string]
BLANK = "  "

BOX = Mailbox(
    host="smtp.example.com", port=587, user=SENDER, password=SecretStr(PASSWORD), to=OWNER
)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install_fake_smtp(monkeypatch)


# --- a message that goes --------------------------------------------------------


def test_the_connection_is_encrypted_before_the_password_is_sent(server: FakeServer) -> None:
    mail.send(BOX, subject="A subject", body="A body.\n")

    assert server.steps == ["connect", "starttls", "login", "send", "close"]
    assert server.connected_to == ("smtp.example.com", 587, mail.TIMEOUT_SECONDS)
    assert server.logged_in_as == (SENDER, PASSWORD)


def test_the_servers_certificate_is_checked(server: FakeServer) -> None:
    """The reason a context is passed at all. Left to itself, ``starttls`` uses one
    with both checks off: the connection is encrypted to whoever answered."""
    mail.send(BOX, subject="A subject", body="A body.\n")

    assert server.context is not None
    assert server.context.verify_mode is ssl.CERT_REQUIRED
    assert server.context.check_hostname is True


def test_the_message_says_who_it_is_from_and_to_and_carries_the_body(server: FakeServer) -> None:
    mail.send(BOX, subject="mihaylov.io chat: 3 questions", body="## Volume\n\n- 3 questions\n")

    [message] = server.sent
    assert message["Subject"] == "mihaylov.io chat: 3 questions"
    assert message["From"] == SENDER
    assert message["To"] == OWNER
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "## Volume\n\n- 3 questions\n"


def test_the_message_is_dated_and_has_an_id_on_the_senders_domain(server: FakeServer) -> None:
    """Gmail adds both when they are missing; another server may not. And the id is
    made on the sender's domain, not on this machine's host name, which is what
    make_msgid uses when it is not told otherwise."""
    mail.send(BOX, subject="A subject", body="A body.\n")

    [message] = server.sent
    assert parsedate_to_datetime(message["Date"]).tzinfo is not None
    assert message["Message-ID"].endswith("@example.com>")
    assert socket.gethostname().lower() not in message["Message-ID"].lower()


def test_a_body_with_characters_outside_ascii_arrives_as_it_was_written(
    server: FakeServer,
) -> None:
    """An article's title can hold anything. ``set_content`` picks UTF-8 and an
    encoding for the wire by itself; ``get_content`` undoes both."""
    body = (
        "- 4  Caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{EM DASH} \N{CYRILLIC CAPITAL LETTER EM}\n"
    )

    mail.send(BOX, subject="A subject", body=body)

    assert server.sent[0].get_content() == body
    assert server.sent[0].get_content_charset() == "utf-8"


# --- a message that does not -----------------------------------------------------


def test_a_rejected_login_is_a_configuration_error_that_names_the_settings(
    server: FakeServer,
) -> None:
    server.fail_login = smtplib.SMTPAuthenticationError(
        535, b"5.7.8 Username and Password not accepted for " + SENDER.encode()
    )

    with pytest.raises(ConfigError) as refusal:
        mail.send(BOX, subject="A subject", body="A body.\n")

    message = str(refusal.value)
    assert "(code 535)" in message, "a 454 is a server asking to be tried later, not a typo"
    assert "SMTP_USER" in message
    assert "SMTP_APP_PASSWORD" in message
    assert "app password" in message
    assert SENDER not in message, "the server's words are not repeated"
    assert PASSWORD not in message
    assert server.sent == []
    assert server.steps[-1] == "close", "the connection is closed on the way out"


@pytest.mark.parametrize(
    ("failure", "says"),
    [
        (
            smtplib.SMTPRecipientsRefused({OWNER: (550, b"5.1.1 no such user " + OWNER.encode())}),
            "SMTPRecipientsRefused",
        ),
        (smtplib.SMTPDataError(554, b"5.7.1 message refused"), "SMTPDataError, code 554"),
        (
            smtplib.SMTPServerDisconnected("Connection unexpectedly closed"),
            "SMTPServerDisconnected",
        ),
    ],
    ids=["recipient-refused", "message-refused", "disconnected"],
)
def test_a_refusal_says_what_kind_it_was_and_repeats_no_address(
    server: FakeServer, failure: Exception, says: str
) -> None:
    """An SMTP refusal quotes the addresses it is about, and this message is logged."""
    server.fail_send = failure

    with pytest.raises(MailError) as refusal:
        mail.send(BOX, subject="A subject", body="A body.\n")

    message = str(refusal.value)
    assert message == f"The email could not be sent ({says})."
    assert OWNER not in message
    assert refusal.value.__cause__ is failure, "the original is kept for whoever debugs it"


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError("timed out"),
        ssl.SSLCertVerificationError(1, "certificate verify failed: hostname mismatch"),
    ],
    ids=["refused", "timed-out", "wrong-certificate"],
)
def test_a_server_that_cannot_be_reached_is_a_mail_error_too(
    server: FakeServer, failure: Exception
) -> None:
    """All three are OSError underneath, which is why one clause catches the network,
    the clock and the certificate."""
    server.fail_connect = failure

    with pytest.raises(MailError, match=type(failure).__name__):
        mail.send(BOX, subject="A subject", body="A body.\n")

    assert server.steps == []


def test_a_login_smtplib_cannot_encode_is_a_mail_error_and_not_a_traceback(
    server: FakeServer,
) -> None:
    """smtplib encodes the name and the password as ASCII before it sends them, and
    raises this for a letter outside it. It is neither an SMTP error nor an OSError,
    so it needs its own name in the except clause."""
    server.fail_login = UnicodeEncodeError(
        "ascii", "pass\N{LATIN SMALL LETTER E WITH ACUTE}", 4, 5, "x"
    )

    with pytest.raises(MailError, match="UnicodeEncodeError"):
        mail.send(BOX, subject="A subject", body="A body.\n")

    assert server.sent == []


def test_a_bug_is_not_dressed_up_as_a_mail_error(server: FakeServer) -> None:
    server.fail_send = TypeError("a programming mistake")

    with pytest.raises(TypeError):
        mail.send(BOX, subject="A subject", body="A body.\n")


# --- the settings ----------------------------------------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()


def test_with_nothing_set_there_is_no_mailbox_and_the_three_settings_are_named() -> None:
    assert mail.mailbox() is None
    assert mail.missing_settings() == ["SMTP_USER", "SMTP_APP_PASSWORD", "DIGEST_TO_EMAIL"]


def test_one_missing_setting_is_enough_for_no_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, smtp_user=SENDER, digest_to_email=OWNER)

    assert mail.mailbox() is None
    assert mail.missing_settings() == ["SMTP_APP_PASSWORD"]


def test_a_blank_line_copied_from_the_example_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, smtp_user=SENDER, smtp_app_password=BLANK, digest_to_email=OWNER)

    assert mail.missing_settings() == ["SMTP_APP_PASSWORD"]


def test_with_all_three_set_the_mailbox_is_gmails_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, smtp_user=SENDER, smtp_app_password=PASSWORD, digest_to_email=OWNER)

    box = mail.mailbox()

    assert mail.missing_settings() == []
    assert box is not None
    assert (box.host, box.port) == ("smtp.gmail.com", 587)
    assert (box.user, box.to) == (SENDER, OWNER)
    assert box.password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(box), "a SecretStr, so printing the mailbox does not print it"
