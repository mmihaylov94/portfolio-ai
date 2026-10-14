"""A stand-in for a mail server, for tests of anything that sends.

``smtplib.SMTP`` is replaced by an object that records the conversation instead of
having it: which host, in what order the steps came, and the message handed over.
A test can also tell it to fail at a step, with the exception the real server's
refusal would raise.

A message is recorded twice. ``sent`` holds the ``EmailMessage`` the caller built.
``delivered`` holds what a server would have been given for it: the envelope (who
sends, and who receives) and the bytes, both worked out by ``smtplib``'s own
``send_message``. The two can disagree, and the second is the truth: an address
header that looks like one mailbox and is read as two, a subject that is one string
and becomes two header lines. A test of what goes into a header reads ``delivered``.

Importable from both suites because pyproject.toml puts ``tests/`` on pytest's
``pythonpath``, as it is for ``openai_fake.py``.
"""

import asyncio
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from types import TracebackType
from typing import Any, cast

import pytest

# The real class, taken while the name still means it: a test replaces
# `smtplib.SMTP` with the stand-in below, which borrows the real send_message.
_RealSMTP = smtplib.SMTP
assert isinstance(_RealSMTP, type), "smtp_fake was imported after smtplib.SMTP was replaced"


@dataclass(frozen=True)
class Delivery:
    """What a mail server is given for one message: an envelope and bytes."""

    sender: str
    # Who the server is told to deliver to. Read from the address headers by
    # smtplib, so one header can make more than one of these.
    recipients: tuple[str, ...]
    wire: bytes

    @property
    def header_names(self) -> list[str]:
        """The names of the headers a reader's mail program would find, in order."""
        return list(BytesParser(policy=policy.default).parsebytes(self.wire).keys())

    @property
    def subject(self) -> str:
        return str(BytesParser(policy=policy.default).parsebytes(self.wire)["Subject"])


class FakeServer:
    """Stands in for both the ``smtplib.SMTP`` class and the object it makes."""

    def __init__(self) -> None:
        # The steps taken, in order: "connect", "starttls", "login", "send", "close".
        self.steps: list[str] = []
        self.connected_to: tuple[str, int, float] | None = None
        self.context: ssl.SSLContext | None = None
        self.logged_in_as: tuple[str, str] | None = None
        # Each message as the object the caller built, and as what a server would
        # have been given for it.
        self.sent: list[EmailMessage] = []
        self.delivered: list[Delivery] = []
        self.extensions = {"smtputf8"}
        # Set to an exception to have that step raise it.
        self.fail_connect: Exception | None = None
        self.fail_login: Exception | None = None
        self.fail_send: Exception | None = None

    # --- called as the class: smtplib.SMTP(host, port, timeout=...) ---------------

    def __call__(self, host: str, port: int, *, timeout: float) -> "FakeServer":
        # smtplib blocks, so nothing may connect from a thread whose event loop is
        # running: the digest sends only after its loop has finished, and the contact
        # form sends from a worker thread (asyncio.to_thread), which has no loop.
        # get_running_loop() raises RuntimeError when the calling thread has none,
        # which is the case this wants.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("a mail server was connected to from inside the event loop")

        if self.fail_connect is not None:
            raise self.fail_connect
        self.steps.append("connect")
        self.connected_to = (host, port, timeout)
        return self

    # --- used as the connection: with ... as server ------------------------------

    def __enter__(self) -> "FakeServer":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        # Returning None lets an exception raised inside the block carry on out,
        # as the real one does.
        self.steps.append("close")

    def starttls(self, *, context: ssl.SSLContext) -> None:
        self.steps.append("starttls")
        self.context = context

    def login(self, user: str, password: str) -> None:
        if self.fail_login is not None:
            raise self.fail_login
        self.steps.append("login")
        self.logged_in_as = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        self.steps.append("send")
        self.sent.append(message)
        # The object above is not what a mail server is given. smtplib works out an
        # envelope and a run of bytes from it, and those can differ from what the
        # object looks like: an address header that reads as two recipients, a
        # subject that becomes two header lines. So the real send_message does that
        # work here too, with this stand-in as its `self`, and hands the result to
        # sendmail below.
        cast("Any", _RealSMTP).send_message(self, message)

    # --- what the real send_message asks of the connection ------------------------

    def ehlo_or_helo_if_needed(self) -> None:
        """The greeting. Nothing to say to nobody."""

    def has_extn(self, name: str) -> bool:
        """Whether the server said it can do ``name``. Asked about SMTPUTF8, for an
        address outside ASCII; Gmail's server can."""
        return name.lower() in self.extensions

    def sendmail(
        self, from_addr: str, to_addrs: Sequence[str], msg: bytes, *_options: object
    ) -> dict[str, tuple[int, bytes]]:
        """What smtplib calls with the envelope and the bytes it worked out. The real
        one returns the recipients the server refused: here, none."""
        self.delivered.append(Delivery(from_addr, tuple(to_addrs), bytes(msg)))
        return {}


def install_fake_smtp(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Route every ``smtplib.SMTP(...)`` in the process to a new fake.

    Patched on the ``smtplib`` module itself. ``mail.py`` writes
    ``smtplib.SMTP``, which looks the name up on that module each time it runs, so
    it finds the fake; had it written ``from smtplib import SMTP``, it would hold
    its own reference to the real class and this would change nothing. A test that
    installs this cannot reach a real mail server.
    """
    server = FakeServer()
    monkeypatch.setattr(smtplib, "SMTP", server)
    return server
