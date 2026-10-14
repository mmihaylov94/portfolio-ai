"""Sending one email, with nothing but the standard library.

``smtplib`` speaks SMTP, the protocol mail servers have used since 1982, and
``email.message.EmailMessage`` builds the message it carries. No mail vendor and no
SDK: a weekly digest and the occasional contact form message do not need either.

Two callers. ``analytics digest`` sends the week's counts to the owner, and the
contact form (``contact/service.py``) sends the owner each message that arrives and
the visitor an acknowledgement. It began in ``analytics/`` and moved up here when
the second one arrived.

**This module is synchronous on purpose.** Everything else in the project that waits
on a network is ``async``, and ``smtplib`` is not: its calls block until the server
answers. Called from inside the event loop, a blocking call stops every other task
for as long as it takes, which is the trap lesson 5 is about. The two callers avoid
it in the two ways there are:

- The digest command does its database and OpenAI work inside the loop, lets the
  loop finish, and only then calls :func:`send`, from ordinary code with nothing
  else to hold up.
- The contact form runs inside the API server, whose loop never finishes, so it
  calls ``asyncio.to_thread(send, ...)``: the blocking function runs on a worker
  thread, and the loop goes on serving chat answers while the mail server replies.

**What a message holds is the caller's business.** The digest composes its own from
the form of the report that holds counts and no words. The contact form's
notification holds what a visitor wrote, because delivering that is its job. Neither
is ever in what this module raises or logs.
"""

import smtplib
import ssl
from dataclasses import dataclass
from email.errors import MessageError
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid

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


def missing_settings(recipient: str, to: str | None) -> list[str]:
    """The names of the settings a send needs and does not have, as ``.env`` spells them.

    The account is this module's; who receives is the caller's own setting, so it
    hands over that setting's name and what it holds: ``("DIGEST_TO_EMAIL",
    settings.digest_to_email)``.
    """
    settings = get_settings()
    needed = {
        "SMTP_USER": settings.smtp_user,
        "SMTP_APP_PASSWORD": settings.smtp_app_password,
        recipient: to,
    }
    return [name for name, value in needed.items() if value is None]


def mailbox(to: str | None) -> Mailbox | None:
    """The sending account with ``to`` as the recipient, or None when anything is missing."""
    settings = get_settings()
    if settings.smtp_user is None or settings.smtp_app_password is None or to is None:
        return None
    return Mailbox(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        password=settings.smtp_app_password,
        to=to,
    )


def _compose(  # ruff: ignore[too-many-arguments] -- every one is a header or a part
    box: Mailbox,
    *,
    subject: str,
    body: str,
    html: str | None,
    reply_to: str | None,
    from_name: str | None,
) -> EmailMessage:
    """The message itself, before any server is involved.

    Raises ``ValueError`` for a header that cannot be one: ``EmailMessage`` refuses a
    value with a line break in it, which is how a second header is smuggled into a
    message, and :func:`_check_headers` refuses one the library would send as
    something other than what it was given. The parser can also fail outright on
    text it was not written for, with an ``IndexError``.
    """
    message = EmailMessage()
    message["Subject"] = subject
    # formataddr writes `Name <address>`, quoting or encoding the name when it needs
    # it. With no name it is the bare address.
    message["From"] = formataddr((from_name, box.user)) if from_name else box.user
    message["To"] = box.to
    if reply_to is not None:
        message["Reply-To"] = reply_to
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
    if html is not None:
        # add_alternative turns the message into multipart/alternative: the text above
        # and this, as two versions of one thing. The last version is the preferred
        # one, which is why the HTML is added second.
        message.add_alternative(html, subtype="html")
    _check_headers(message, subject=subject, to=box.to, reply_to=reply_to)
    return message


def _check_headers(message: EmailMessage, *, subject: str, to: str, reply_to: str | None) -> None:
    """Refuse a message whose headers are not the ones that were asked for.

    ``EmailMessage`` does not keep the text it is given: it parses it. One thing its
    parser does is decode "encoded words", the ``=?utf-8?q?...?=`` form mail uses to
    carry characters outside ASCII in a header. Text that holds that form comes out
    as something else: a comma that makes one address two, a line break that starts
    a second header, a link nobody read. A check made on the text beforehand has
    then checked a different string from the one that is sent.

    So this looks afterwards, at what the library holds. The subject has to be the
    subject that was asked for. Each address header has to read as exactly the one
    address given, read the way ``smtplib`` reads them to decide who receives the
    message. Raises ``ValueError``, which :func:`send` reports as a message that
    could not be built.
    """
    if str(message["Subject"]) != subject:
        raise ValueError("the subject would not be sent as it was given")
    for name, given in (("To", to), ("Reply-To", reply_to)):
        if given is None:
            continue
        named = [address for _, address in getaddresses([str(message[name])])]
        if named != [given]:
            raise ValueError(f"the {name} header would not name the one address given")


def send(  # ruff: ignore[too-many-arguments] -- keyword-only, and every one is a header or a part
    box: Mailbox,
    *,
    subject: str,
    body: str,
    html: str | None = None,
    reply_to: str | None = None,
    from_name: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> None:
    """Send one message, or raise: ``ConfigError`` for a rejected login, ``MailError``
    for anything else that stopped it.

    ``body`` is the message as plain text. ``html``, when given, is the same message
    as HTML: the two travel together and the reader's mail program shows the one it
    prefers. ``reply_to`` is where an answer goes when that is not the account that
    sent it, and ``from_name`` is the name shown beside the sending address.
    ``timeout`` is how long to wait on the server at each step, for a caller with
    somebody waiting on it.

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
    try:
        message = _compose(
            box, subject=subject, body=body, html=html, reply_to=reply_to, from_name=from_name
        )
    except (ValueError, LookupError) as exc:
        # The settings check their own addresses. A caller that passes on what a
        # visitor typed has to have checked it too, and this is the net under that.
        # LookupError is the header parser falling over (an IndexError) on text it
        # was not written for, which is a header that cannot be one like any other.
        raise MailError("The email could not be built (a header was refused).") from exc

    try:
        with smtplib.SMTP(box.host, box.port, timeout=timeout) as server:
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
    except (smtplib.SMTPException, OSError, UnicodeError, MessageError) as exc:
        # Three names, for the reader: smtplib.SMTPException is itself an OSError, so
        # the second already covers the first. OSError is also the network: no route,
        # connection refused, a timeout, a certificate that does not verify.
        #
        # UnicodeError is smtplib giving up before it has said anything: it encodes
        # the login as ASCII, and a name or password with a letter outside it raises
        # here, on this machine. The settings refuse such an address; a password is
        # not looked at there.
        #
        # MessageError is the mail library refusing to write a header out as it
        # flattens the message for the server: HeaderWriteError, from Python 3.12.5,
        # whose message quotes the header, a visitor's subject included. The checks in
        # _compose should leave it nothing to refuse; this is the net under them.
        #
        # Only the kind of failure is reported. The server's own words can quote the
        # addresses involved, and this message is logged; the original stays attached
        # as __cause__ for anyone debugging.
        code = getattr(exc, "smtp_code", None)
        detail = f"{type(exc).__name__}, code {code}" if code else type(exc).__name__
        raise MailError(f"The email could not be sent ({detail}).") from exc
