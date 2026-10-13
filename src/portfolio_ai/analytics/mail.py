"""Sending one plain-text email, with nothing but the standard library.

``smtplib`` speaks SMTP, the protocol mail servers have used since 1982, and
``email.message.EmailMessage`` builds the message it carries. No mail vendor and no
SDK: four emails a month to one person do not need either.

**This module is synchronous on purpose.** Everything else in the project that waits
on a network is ``async``, and ``smtplib`` is not: its calls block until the server
answers. Called from inside the event loop, a blocking call stops every other task
for as long as it takes, which is the trap lesson 5 is about. So the digest command
does its database and OpenAI work inside the loop, lets the loop finish, and only
then calls :func:`send`, from ordinary code with nothing else to hold up. The other
way round would be ``asyncio.to_thread(send, ...)``, which runs a blocking function
on a worker thread. It is the right tool inside a server, where the loop has other
work, and more machinery than one command needs.

What a visitor typed never comes through here. The caller composes the message from
the form of the report that holds counts and no words (``report.render`` with
``verbatim=False``).
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from pydantic import SecretStr

from portfolio_ai.config import get_settings
from portfolio_ai.exceptions import ConfigError, MailError

# How long to wait on the mail server at each step. smtplib's own default is to wait
# for ever, and a scheduled job that never ends is worse than one that fails.
TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Mailbox:
    """Everything needed to send: the server, the account, and who receives."""

    host: str
    port: int
    user: str
    password: SecretStr
    to: str


def missing_settings() -> list[str]:
    """The names of the settings a send needs and does not have, as ``.env`` spells them."""
    settings = get_settings()
    needed = {
        "SMTP_USER": settings.smtp_user,
        "SMTP_APP_PASSWORD": settings.smtp_app_password,
        "DIGEST_TO_EMAIL": settings.digest_to_email,
    }
    return [name for name, value in needed.items() if value is None]


def mailbox() -> Mailbox | None:
    """The mail settings, or None when any of them is missing."""
    settings = get_settings()
    if (
        settings.smtp_user is None
        or settings.smtp_app_password is None
        or settings.digest_to_email is None
    ):
        return None
    return Mailbox(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_app_password,
        to=settings.digest_to_email,
    )


def send(box: Mailbox, *, subject: str, body: str) -> None:
    """Send one plain-text message, or raise: ``ConfigError`` for a rejected login,
    ``MailError`` for anything else that stopped it.

    The conversation with the server, in order:

    1. ``smtplib.SMTP(host, port)`` connects, in plain text. On port 587 that is how
       it starts.
    2. ``starttls`` upgrades the connection to TLS. ``ssl.create_default_context()``
       is what makes that mean something: it checks the server's certificate against
       the system's trusted authorities and checks that it was issued for this host.
       Left out, ``starttls`` falls back to a context with both checks switched off
       (so it is on Python 3.12, where this was looked up). The connection is then
       encrypted to whoever answered, which keeps out an eavesdropper and lets in an
       impostor.
    3. ``login``, which is why step 2 comes first: the password goes over the
       encrypted connection or not at all.
    4. ``send_message`` hands the message over. It reads the sender and recipient
       from the message's own headers.

    ``with`` closes the connection on the way out, politely (``QUIT``) when all went
    well and anyway when it did not.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = box.user
    message["To"] = box.to
    # A message is supposed to say when it was written and carry an id of its own.
    # Gmail's server adds both when they are missing; another SMTP_HOST may not, and a
    # message without them is one a spam filter looks at twice. The id is made on the
    # sender's domain: left to itself make_msgid uses this machine's host name, and a
    # container's name has no business in a mail header.
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = make_msgid(domain=box.user.partition("@")[2])
    # Plain text, as UTF-8. The digest is Markdown, which reads well enough unrendered
    # that turning it into HTML would add a dependency for the sake of some bold.
    message.set_content(body)

    try:
        with smtplib.SMTP(box.host, box.port, timeout=TIMEOUT_SECONDS) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(box.user, box.password.get_secret_value())
            server.send_message(message)
    except smtplib.SMTPAuthenticationError as exc:
        # A configuration problem, so a readable refusal and not a traceback. Gmail
        # answers this way for the account's own password as well as a wrong one. The
        # code says which: 535 is a refused name or password, 534 an account that has
        # to use an app password, and a 454 is the server asking to be tried later.
        raise ConfigError(
            f"The mail server rejected the login (code {exc.smtp_code}). Check SMTP_USER "
            "and SMTP_APP_PASSWORD: Gmail takes an app password, not the account's own."
        ) from exc
    except (smtplib.SMTPException, OSError, UnicodeError) as exc:
        # Three names, for the reader: smtplib.SMTPException is itself an OSError, so
        # the second already covers the first. OSError is also the network: no route,
        # connection refused, a timeout, a certificate that does not verify.
        #
        # UnicodeError is smtplib giving up before it has said anything: it encodes
        # the login as ASCII, and a name or password with a letter outside it raises
        # here, on this machine. The settings refuse such an address; a password is
        # not looked at there.
        #
        # Only the kind of failure is reported. The server's own words can quote the
        # addresses involved, and this message is logged; the original stays attached
        # as __cause__ for anyone debugging.
        code = getattr(exc, "smtp_code", None)
        detail = f"{type(exc).__name__}, code {code}" if code else type(exc).__name__
        raise MailError(f"The email could not be sent ({detail}).") from exc
