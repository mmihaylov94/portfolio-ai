"""Sending an email, with the mail server replaced by something that records.

What matters here is the order of the conversation (encrypt, then log in, then
send), that the certificate is really checked, and what a failure says: enough to
fix it, and never the password or an address, because the message is logged.
"""

import dataclasses
import smtplib
import socket
import ssl
from email.errors import MessageError
from email.utils import parsedate_to_datetime

import pytest
from pydantic import SecretStr
from smtp_fake import FakeServer, install_fake_smtp

from portfolio_ai import mail
from portfolio_ai.config import get_settings
from portfolio_ai.exceptions import ConfigError, MailError
from portfolio_ai.mail import Mailbox

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


def test_a_header_the_library_will_not_write_out_is_a_mail_error_that_quotes_nothing(
    server: FakeServer,
) -> None:
    """From Python 3.12.5 the mail library raises HeaderWriteError, a MessageError,
    when it flattens a header it will not send, and its message quotes the header. The
    checks before it should leave it nothing to refuse; if one ever does, the subject
    it quotes may be a visitor's, so only the kind of failure is passed on."""
    server.fail_send = MessageError("folded header contains newline: 'Subject: secret'")

    with pytest.raises(MailError) as refused:
        mail.send(BOX, subject="A subject", body="A body.\n")

    assert str(refused.value) == "The email could not be sent (MessageError)."
    assert "secret" not in str(refused.value)


def test_a_bug_is_not_dressed_up_as_a_mail_error(server: FakeServer) -> None:
    server.fail_send = TypeError("a programming mistake")

    with pytest.raises(TypeError):
        mail.send(BOX, subject="A subject", body="A body.\n")


# --- what the contact form needs of a message ---------------------------------------


def test_a_message_can_carry_a_name_a_reply_address_and_an_html_version(
    server: FakeServer,
) -> None:
    """The digest uses none of these. The contact form's acknowledgement uses all
    three: it goes out under the owner's name, from the sending account, with his
    own address as where an answer goes, as text and as HTML."""
    mail.send(
        BOX,
        subject="A subject",
        body="Hello,\n\nPlain.\n",
        html="<p>Hello,</p><p>Marked up.</p>",
        reply_to="replies@example.com",
        from_name="Mihail Mihaylov",
    )

    [message] = server.sent
    assert message["From"] == f"Mihail Mihaylov <{SENDER}>"
    assert message["Reply-To"] == "replies@example.com"
    assert message.get_content_type() == "multipart/alternative"
    plain, marked_up = message.iter_parts()
    assert (plain.get_content_type(), marked_up.get_content_type()) == ("text/plain", "text/html")
    assert plain.get_content() == "Hello,\n\nPlain.\n"
    assert "<p>Marked up.</p>" in marked_up.get_content()


def test_without_them_a_message_is_what_it_always_was(server: FakeServer) -> None:
    mail.send(BOX, subject="A subject", body="A body.\n")

    [message] = server.sent
    assert message["From"] == SENDER
    assert message["Reply-To"] is None
    assert message.get_content_type() == "text/plain"


@pytest.mark.parametrize(
    ("subject", "reply_to"),
    [
        ("A subject\nBcc: other@example.com", None),
        ("A subject", "replies@example.com\r\nBcc: other@example.com"),
    ],
    ids=["subject", "reply-to"],
)
def test_a_header_with_a_line_break_in_it_is_refused_before_any_server_is_reached(
    server: FakeServer, subject: str, reply_to: str | None
) -> None:
    """A line break is how a second header is smuggled into a message. The contact
    form puts a visitor's subject in one; its own cleaning removes line breaks, and
    this is the net under that. A mail error, like any other failure to send, and
    with nothing of the value in it."""
    with pytest.raises(MailError) as refused:
        mail.send(BOX, subject=subject, body="A body.\n", reply_to=reply_to)

    assert str(refused.value) == "The email could not be built (a header was refused)."
    assert server.steps == []


# "Encoded words": the form mail uses to carry characters outside ASCII in a header.
# EmailMessage decodes one in any header it is handed, so each of these is one thing
# as text and another once it is a header: two recipients, a second header line, a
# link, and a word the parser falls over on.
TWO_RECIPIENTS = "=?utf-8?q?first=40example.org=2C?=second@example.net"
SECOND_HEADER = "=?utf-8?q?Hi=0D=0AX-Injected:_yes?="
HIDDEN_LINK = "=?utf-8?b?aHR0cHM6Ly9ldmlsLmV4YW1wbGUvcGF5?="
BROKEN_WORD = "=?utf-8?q??=@example.com"


@pytest.mark.parametrize(
    ("subject", "to", "reply_to"),
    [
        (SECOND_HEADER, OWNER, None),
        (HIDDEN_LINK, OWNER, None),
        ("A subject", TWO_RECIPIENTS, None),
        ("A subject", BROKEN_WORD, None),
        ("A subject", "a,b@example.com", None),
        ("A subject", "Somebody <somebody@example.com>", None),
        ("A subject", OWNER, TWO_RECIPIENTS),
    ],
    ids=[
        "a-subject-that-decodes-to-two-lines",
        "a-subject-that-decodes-to-a-link",
        "an-address-that-decodes-to-two",
        "an-address-the-parser-cannot-read",
        "two-addresses",
        "a-name-and-an-address",
        "a-reply-address-that-decodes-to-two",
    ],
)
def test_a_header_the_library_would_send_as_something_else_is_refused(
    server: FakeServer, subject: str, to: str, reply_to: str | None
) -> None:
    """The check is made on what the library holds once the header is built, and not
    on the text handed to it: by then an encoded word has been decoded. This is the
    net under the contact form's own checks, and it does not depend on which Python
    is running: a later 3.12 release may write some of these out safely by itself,
    and this refuses them all on any release."""
    box = dataclasses.replace(BOX, to=to)

    with pytest.raises(MailError) as refused:
        mail.send(box, subject=subject, body="A body.\n", reply_to=reply_to)

    assert str(refused.value) == "The email could not be built (a header was refused)."
    assert server.steps == [], "no server was reached"


def test_what_a_server_is_given_is_one_recipient_and_the_headers_asked_for(
    server: FakeServer,
) -> None:
    """The envelope and the bytes, as smtplib works them out. A subject outside
    ASCII is carried as an encoded word, which is the library writing one for a
    good reason, and it reads back as what was given."""
    subject = "Caf" + chr(0xE9) + " at eight"

    mail.send(
        BOX,
        subject=subject,
        body="A body.\n",
        reply_to="replies@example.com",
        from_name="Mihail Mihaylov",
    )

    [delivery] = server.delivered
    assert (delivery.sender, delivery.recipients) == (SENDER, (OWNER,))
    assert delivery.header_names == [
        "Subject",
        "From",
        "To",
        "Reply-To",
        "Date",
        "Message-ID",
        "Content-Type",
        "Content-Transfer-Encoding",
        "MIME-Version",
    ]
    assert delivery.subject == subject
    assert b"=?utf-8?" in delivery.wire.split(b"\r\n\r\n")[0]


def test_a_caller_with_somebody_waiting_can_give_the_server_less_time(server: FakeServer) -> None:
    mail.send(BOX, subject="A subject", body="A body.\n", timeout=6.0)

    assert server.connected_to == ("smtp.example.com", 587, 6.0)


def test_a_test_that_forgets_the_stand_in_cannot_reach_a_mail_server() -> None:
    """No ``server`` fixture here, on purpose. tests/unit/conftest.py replaces
    ``smtplib.SMTP`` for every unit test with something that fails the test, after
    one test of the contact form opened a real connection to the mail server named
    in the default settings."""
    with pytest.raises(AssertionError, match="tried to connect to a mail server"):
        mail.send(BOX, subject="A subject", body="A body.\n")


# --- the settings ----------------------------------------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name.upper(), value)
    get_settings.cache_clear()


def _digest_box() -> Mailbox | None:
    return mail.mailbox(get_settings().digest_to_email)


def _digest_missing() -> list[str]:
    return mail.missing_settings("DIGEST_TO_EMAIL", get_settings().digest_to_email)


def test_with_nothing_set_there_is_no_mailbox_and_the_three_settings_are_named() -> None:
    assert _digest_box() is None
    assert _digest_missing() == ["SMTP_USER", "SMTP_APP_PASSWORD", "DIGEST_TO_EMAIL"]


def test_the_recipient_is_the_callers_own_setting_and_is_named_as_the_caller_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The account belongs to this module. Who receives is each caller's setting: the
    digest's is DIGEST_TO_EMAIL, the contact form's CONTACT_TO_EMAIL, and a missing
    one is reported by the name its own reader will look for in .env."""
    _configure(monkeypatch, smtp_user=SENDER, smtp_app_password=PASSWORD)

    assert mail.missing_settings("CONTACT_TO_EMAIL", None) == ["CONTACT_TO_EMAIL"]
    assert mail.missing_settings("CONTACT_TO_EMAIL", OWNER) == []
    assert mail.mailbox(None) is None
    box = mail.mailbox(OWNER)
    assert box is not None
    assert (box.user, box.to) == (SENDER, OWNER)


def test_one_missing_setting_is_enough_for_no_mailbox(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, smtp_user=SENDER, digest_to_email=OWNER)

    assert _digest_box() is None
    assert _digest_missing() == ["SMTP_APP_PASSWORD"]


def test_a_blank_line_copied_from_the_example_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, smtp_user=SENDER, smtp_app_password=BLANK, digest_to_email=OWNER)

    assert _digest_missing() == ["SMTP_APP_PASSWORD"]


def test_with_all_three_set_the_mailbox_is_gmails_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, smtp_user=SENDER, smtp_app_password=PASSWORD, digest_to_email=OWNER)

    box = _digest_box()

    assert _digest_missing() == []
    assert box is not None
    assert (box.host, box.port) == ("smtp.gmail.com", 587)
    assert (box.user, box.to) == (SENDER, OWNER)
    assert box.password.get_secret_value() == PASSWORD
    assert PASSWORD not in repr(box), "a SecretStr, so printing the mailbox does not print it"
