"""A stand-in for a mail server, for tests of anything that sends.

``smtplib.SMTP`` is replaced by an object that records the conversation instead of
having it: which host, in what order the steps came, and the message handed over.
A test can also tell it to fail at a step, with the exception the real server's
refusal would raise.

Importable from both suites because pyproject.toml puts ``tests/`` on pytest's
``pythonpath``, as it is for ``openai_fake.py``.
"""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from types import TracebackType

import pytest


class FakeServer:
    """Stands in for both the ``smtplib.SMTP`` class and the object it makes."""

    def __init__(self) -> None:
        # The steps taken, in order: "connect", "starttls", "login", "send", "close".
        self.steps: list[str] = []
        self.connected_to: tuple[str, int, float] | None = None
        self.context: ssl.SSLContext | None = None
        self.logged_in_as: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        # Set to an exception to have that step raise it.
        self.fail_connect: Exception | None = None
        self.fail_login: Exception | None = None
        self.fail_send: Exception | None = None

    # --- called as the class: smtplib.SMTP(host, port, timeout=...) ---------------

    def __call__(self, host: str, port: int, *, timeout: float) -> "FakeServer":
        # smtplib blocks, so nothing may connect while an event loop is running: the
        # digest sends only after its loop has finished. get_running_loop() raises
        # RuntimeError when there is none, which is the case this wants.
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


def install_fake_smtp(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    """Route every ``smtplib.SMTP(...)`` in the process to a new fake.

    Patched on the ``smtplib`` module itself. ``analytics/mail.py`` writes
    ``smtplib.SMTP``, which looks the name up on that module each time it runs, so
    it finds the fake; had it written ``from smtplib import SMTP``, it would hold
    its own reference to the real class and this would change nothing. A test that
    installs this cannot reach a real mail server.
    """
    server = FakeServer()
    monkeypatch.setattr(smtplib, "SMTP", server)
    return server
