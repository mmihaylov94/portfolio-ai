"""One contact form message, from four fields to the emails, with nothing real behind it.

OpenAI is ``tests/openai_fake.py`` and the mail server is ``tests/smtp_fake.py``, so
every test here runs the real flow: the rules, the two calls, the checks on what the
models return, and the sending.

Two things the mail stand-in does matter more than they look. It records every
message twice: as the object the flow built (``sent``), and as what a mail server
would have been given for it, an envelope and bytes (``delivered``). The second is
what a test of a header reads, because a header can be one thing as an object and
another on the wire. And it refuses a connection made from a thread whose event loop
is running, which is what pins ``asyncio.to_thread``: ``handle`` is a coroutine, and
a send made straight from it would stop the API's loop for as long as the mail
server took.
"""

import asyncio
import threading
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

import pytest
from log_recorder import LogRecorder
from openai_fake import FakeOpenAI, install_fake_openai
from smtp_fake import FakeServer, install_fake_smtp

from portfolio_ai import mail
from portfolio_ai.assistant.prompts.loader import (
    CONTACT_BRIEFS,
    CONTACT_CLASSIFIER,
    CONTACT_CONTEXT,
    CONTACT_REPLY,
)
from portfolio_ai.config import get_settings
from portfolio_ai.contact import classifier, emails, reply, rules, service
from portfolio_ai.contact.replies import Replies
from portfolio_ai.exceptions import ConfigError, MailError
from portfolio_ai.llm import responses

SENDER = "sender@example.com"
OWNER = "owner@example.com"
VISITOR = "visitor@example.com"
# Made up, in the shape Gmail gives an app password.
PASSWORD = "abcd efgh ijkl mnop"  # ruff: ignore[hardcoded-password-string]

SUBJECT = "A tool for our warehouse"
MESSAGE = "We track stock in spreadsheets and would like a small web tool for our team."
FIRST = "Thank you for getting in touch about the warehouse tool."
SECOND = "Could you tell me how many people would use it, and by when you need it?"
EXPLANATION = "A specific request to build an internal tool."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    return install_fake_smtp(monkeypatch)


@pytest.fixture(autouse=True)
def mail_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_USER", SENDER)
    monkeypatch.setenv("SMTP_APP_PASSWORD", PASSWORD)
    monkeypatch.setenv("CONTACT_TO_EMAIL", OWNER)
    get_settings.cache_clear()


async def _handle(replies: Replies | None = None, /, **fields: str) -> service.Outcome:
    """One submission, with a count of replies of its own unless one is handed in.

    ``replies`` is positional-only (the ``/``), so that no field of the form, passed
    as a keyword, can be taken for it.
    """
    given = {
        "email": VISITOR,
        "subject": SUBJECT,
        "message": MESSAGE,
        "reason": "project",
        **fields,
    }
    return await service.handle(**given, replies=replies if replies is not None else Replies())


def _text(message: EmailMessage) -> str:
    """The plain-text version of a message that has an HTML one beside it."""
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return str(part.get_content())


def _html(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("html",))
    assert part is not None
    return str(part.get_content())


# --- a message that is let through and acknowledged ----------------------------------


async def test_a_real_inquiry_is_acknowledged_and_then_passed_on(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    fake.verdict(explanation=EXPLANATION)
    fake.paragraphs(FIRST, SECOND)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged, outcome.spam_score) == ("accepted", True, 0)
    assert outcome.cost > 0
    # What the mail server was given for each: one recipient, and no other.
    assert [(delivery.sender, delivery.recipients) for delivery in server.delivered] == [
        (SENDER, (VISITOR,)),
        (SENDER, (OWNER,)),
    ]
    # Six seconds a step for these, and not the half minute a scheduled job is given.
    settings = get_settings()
    assert server.connected_to == (settings.smtp_host, settings.smtp_port, 6.0)
    acknowledgement, notification = server.sent

    # To the visitor: from the sending account under his name, with the owner's
    # address as where an answer goes, and a subject of its own: nothing the visitor
    # typed goes into an email sent to a visitor.
    assert acknowledgement["To"] == VISITOR
    assert parseaddr(acknowledgement["From"]) == ("Mihail Mihaylov", SENDER)
    assert acknowledgement["Reply-To"] == OWNER
    assert acknowledgement["Subject"] == "Your message to Mihail Mihaylov"
    assert _text(acknowledgement) == (
        f"Hello,\n\n{FIRST}\n\n{SECOND}\n\nKind Regards,\nMihail\n\n"
        f"{emails.AUTOMATIC}\n{emails.NOT_YOU}\n"
    )
    assert emails.NOT_YOU in _html(acknowledgement)

    # To the owner, afterwards: everything about it, the reply included.
    assert notification["To"] == OWNER
    # parseaddr, and not a comparison of the header as written: a name with a dot in
    # it is put in quotes on the way out, as the mail format requires.
    assert parseaddr(notification["From"]) == ("mihaylov.io contact form", SENDER)
    assert notification["Subject"] == (
        f"mihaylov.io - VALID Contact Form Submission [AUTO-REPLIED] - {SUBJECT}"
    )
    body = _text(notification)
    for line in (
        "Status: Accepted",
        "Auto-reply sent: Yes",
        f"Sender Email: {VISITOR}",
        "Submitted Reason: project",
        "Detected Reason: project",
        f"Message: {MESSAGE}",
        "Spam Score: 0",
        "Rule Hits: None",
        "Spam Confidence: 0.05",
        "Reason Confidence: 0.9",
        f"AI Explanation: {EXPLANATION}",
        FIRST,
        emails.AUTOMATIC,
    ):
        assert line in body


@pytest.mark.usefixtures("server")
async def test_the_two_models_are_asked_with_the_ported_prompts(fake: FakeOpenAI) -> None:
    """The classifier's model and effort for both, as n8n used one small model for
    both. Each call names its prompt, so its cost can be traced to a version."""
    fake.verdict(detected_reason="consultation")
    fake.paragraphs(FIRST)

    await _handle(reason="project")

    asked_classifier, asked_reply = fake.requests
    for request in (asked_classifier, asked_reply):
        assert request["model"] == "gpt-5-mini"
        assert request["reasoning"] == {"effort": "minimal"}
    assert asked_classifier["instructions"] == CONTACT_CLASSIFIER.text
    assert asked_reply["instructions"] == CONTACT_REPLY.text
    # The brief is the one for what the classifier found, not for what was ticked.
    sent = asked_reply["input"][0]["content"]
    assert sent.startswith("Write a reply to a consultation request.")
    assert CONTACT_CONTEXT.text in sent
    assert f"- Message: {MESSAGE}" in sent


# --- when no acknowledgement goes out --------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "fields", "why"),
    [
        ({"should_auto_reply": False}, {}, "The classifier advised against an automatic reply."),
        (
            {"reason_confidence": 0.59},
            {},
            "The classifier was not sure enough what the message is about.",
        ),
        (
            {"spam_confidence": 0.31},
            {},
            "The classifier was not sure enough that the message is not spam.",
        ),
        ({}, {"message": "seo??"}, "The fixed rules found the message suspicious."),
        (
            {},
            {"email": "a,b@example.com"},
            "The sender's address is not one a reply can safely be sent to.",
        ),
    ],
    ids=["advised-against", "unsure-of-the-reason", "might-be-spam", "suspicious", "address"],
)
async def test_an_accepted_message_is_not_acknowledged_when(
    fake: FakeOpenAI,
    server: FakeServer,
    verdict: dict[str, object],
    fields: dict[str, str],
    why: str,
) -> None:
    """The workflow's four conditions, and one more: an address a mail header would
    read as two recipients. The reply is not even written, so no second call is
    paid for, and the owner is told which condition it was."""
    fake.verdict(**verdict)  # type: ignore[arg-type]

    outcome = await _handle(**fields)

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert len(fake.requests) == 1, "the classifier was asked, and nothing was written"
    [notification] = server.sent
    assert notification["To"] == OWNER
    assert "[NO AUTO-REPLY]" in notification["Subject"]
    assert "Auto-reply sent: No" in _text(notification)
    assert f"Reply: {why}" in _text(notification)


async def test_a_verdict_exactly_on_both_thresholds_is_acknowledged(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """At least 0.6 sure of the reason, and at most 0.3 that it is spam."""
    fake.verdict(reason_confidence=0.6, spam_confidence=0.3)
    fake.paragraphs(FIRST)

    assert (await _handle()).acknowledged
    assert len(server.sent) == 2


# --- what the rules and the classifier stop ---------------------------------------------


async def test_an_invalid_submission_costs_nothing_and_is_reported_as_blocked(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    outcome = await _handle(reason="sales")

    assert (outcome.status, outcome.acknowledged) == ("invalid", False)
    assert not outcome.cost, "nothing was spent"
    assert fake.requests == [], "no model is asked about what the rules refuse"
    [notification] = server.sent
    assert notification["Subject"] == (
        f"mihaylov.io - BLOCKED Contact Form Submission [INVALID] - {SUBJECT}"
    )
    body = _text(notification)
    assert "Status: Invalid request" in body
    assert "Why blocked: Invalid reason value" in body
    assert "Auto-reply sent: No" in body


async def test_spam_by_the_rules_is_blocked_without_asking_a_model(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    outcome = await _handle(email="someone@mailinator.com", message="seo??")

    assert (outcome.status, outcome.spam_score) == ("rule_spam", 6)
    assert fake.requests == []
    [notification] = server.sent
    assert "[RULE_SPAM]" in notification["Subject"]
    body = _text(notification)
    assert "Status: Blocked by rule-based spam checks" in body
    assert "Why blocked: Rule-based spam threshold reached" in body
    assert "- Disposable email domain\n- Message too short\n- Spam keyword hits: 1" in body


async def test_spam_by_the_classifier_is_blocked_and_gets_no_reply(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    fake.verdict(is_spam=True, spam_confidence=0.95, explanation="Unsolicited SEO outreach.")

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("ai_spam", False)
    assert len(fake.requests) == 1
    [notification] = server.sent
    assert "[AI_SPAM]" in notification["Subject"]
    body = _text(notification)
    assert "Status: Blocked by AI classifier" in body
    assert "Why blocked: Unsolicited SEO outreach." in body
    assert "AI Marked as Spam: true" in body


# --- when something fails: the message still arrives -----------------------------------


async def test_a_message_reaches_the_owner_when_openai_cannot_be_reached(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """The n8n workflow lost such a message: its run failed, after the form had
    already said thank you."""
    fake.http_error(500)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("not_classified", False)
    [notification] = server.sent
    assert notification["Subject"] == (
        f"mihaylov.io - VALID Contact Form Submission [NOT CLASSIFIED] - {SUBJECT}"
    )
    body = _text(notification)
    assert "Status: Accepted, not classified" in body
    assert f"Message: {MESSAGE}" in body
    assert "AI Classification" not in body


async def test_a_verdict_that_cannot_be_read_is_treated_as_no_verdict(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    fake.unparseable()

    outcome = await _handle()

    assert outcome.status == "not_classified"
    assert outcome.cost >= 0, "the call was made, and counted"
    assert "[NOT CLASSIFIED]" in server.sent[0]["Subject"]


async def test_a_reply_that_cannot_be_written_does_not_hold_the_message_back(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    fake.verdict()
    fake.http_error(500)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    [notification] = server.sent
    assert "[NO AUTO-REPLY]" in notification["Subject"]
    assert "Reply: The reply could not be written." in _text(notification)


async def _never(*_args: object) -> None:
    """A call to a model that does not come back."""
    await asyncio.Event().wait()


async def test_a_classifier_that_does_not_answer_in_time_is_not_waited_for(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A visitor is watching the form, and the site's API stops waiting after half a
    minute. Left to the OpenAI client's own patience, a slow day at OpenAI would
    show the visitor an error for a message that then arrives."""
    recorder = LogRecorder()
    monkeypatch.setattr(service, "log", recorder)
    monkeypatch.setattr(classifier, "classify", _never)
    monkeypatch.setattr(service, "CLASSIFY_SECONDS", 0.01)

    # A deadline of the test's own, so that a flow with no deadline fails here in
    # five seconds and does not hang the suite.
    async with asyncio.timeout(5):
        outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("not_classified", False)
    [notification] = server.sent
    assert "[NOT CLASSIFIED]" in notification["Subject"]
    [failed] = [line for line in recorder.lines if line["event"] == "contact_classifier_failed"]
    assert failed["error_type"] == "TimeoutError"


async def test_a_reply_that_is_not_written_in_time_is_not_waited_for(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.verdict()
    monkeypatch.setattr(reply, "draft", _never)
    monkeypatch.setattr(service, "REPLY_SECONDS", 0.01)

    async with asyncio.timeout(5):
        outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    [notification] = server.sent
    assert "Reply: The reply could not be written." in _text(notification)


def test_the_deadlines_leave_the_sites_api_time_to_hear_the_answer() -> None:
    """The portfolio's API waits 35 seconds for this endpoint (``contactMs`` in its
    api/src/config.js, which a test there holds to that). Both models at their
    slowest and both emails at theirs come to 30 here. A number changed on one side
    alone fails on that side."""
    assert (service.CLASSIFY_SECONDS, service.REPLY_SECONDS, service.SEND_SECONDS) == (
        8.0,
        10.0,
        6.0,
    )
    assert service.CLASSIFY_SECONDS + service.REPLY_SECONDS + 2 * service.SEND_SECONDS <= 30


# --- an optional step that fails in a way nobody listed ------------------------------------


async def _breaks(*_args: object) -> None:
    """What the OpenAI client does with a 200 that is a page of HTML: an error of
    Python's own, from inside the library, that no list of expected errors holds."""
    await asyncio.sleep(0)
    raise AttributeError("'str' object has no attribute 'output'")


async def test_a_classifier_that_fails_in_a_way_nobody_listed_does_not_lose_the_message(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(classifier, "classify", _breaks)

    outcome = await _handle()

    assert outcome.status == "not_classified"
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]


async def test_a_reply_writer_that_fails_in_a_way_nobody_listed_does_not_lose_the_message(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.verdict()
    monkeypatch.setattr(reply, "draft", _breaks)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]
    assert "Reply: The reply could not be written." in _text(server.sent[0])


async def test_an_acknowledgement_that_fails_in_a_way_nobody_listed_does_not_lose_the_message(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A courtesy that fails must not stop the one email that matters. The mail
    library's header parser can fall over with an IndexError, which is not a
    ``MailError`` and was once enough to end the flow before the owner was told."""
    fake.verdict()
    fake.paragraphs(FIRST)
    real_send = mail.send

    def send(box: mail.Mailbox, **parts: Any) -> None:
        if box.to == VISITOR:
            raise IndexError("list index out of range")
        real_send(box, **parts)

    monkeypatch.setattr(mail, "send", send)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]
    assert "Reply: The reply was written and could not be sent." in _text(server.sent[0])


# --- an email that takes too long ------------------------------------------------------------


async def test_an_acknowledgement_that_takes_too_long_is_not_waited_for(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait has a deadline. The thread doing the sending has not: a thread cannot
    be stopped from outside, so this one is let go here once the test has seen what
    it came for, and it returns without sending anything."""
    fake.verdict()
    fake.paragraphs(FIRST)
    let_go = threading.Event()
    real_send = mail.send

    def send(box: mail.Mailbox, **parts: Any) -> None:
        if box.to == VISITOR:
            let_go.wait(10)
            return
        real_send(box, **parts)

    monkeypatch.setattr(mail, "send", send)
    monkeypatch.setattr(service, "SEND_SECONDS", 0.3)

    try:
        async with asyncio.timeout(5):
            outcome = await _handle()
    finally:
        let_go.set()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]
    assert "Reply: The reply was written, and sending it did not finish in time." in _text(
        server.sent[0]
    )


async def test_a_notification_that_takes_too_long_is_a_mail_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 to the caller, like any other email that did not go, and not the 500 a
    bare TimeoutError would be."""
    let_go = threading.Event()

    def send(box: mail.Mailbox, **parts: Any) -> None:  # ruff: ignore[unused-function-argument]
        let_go.wait(10)

    monkeypatch.setattr(mail, "send", send)
    monkeypatch.setattr(service, "SEND_SECONDS", 0.05)

    try:
        with pytest.raises(MailError, match="took too long"):
            async with asyncio.timeout(5):
                # Blocked by the rules, so there is one email and no model to ask.
                await _handle(reason="sales")
    finally:
        let_go.set()


# --- a header is what the mail library makes of the text -------------------------------------

# "Encoded words": the form mail uses to carry characters outside ASCII in a header.
# The mail library decodes one in any header it is handed. As typed, the first is
# one address; decoded, it is two. The others decode to a second header line and to
# a link.
TWO_RECIPIENTS = "=?utf-8?q?first=40example.org=2C?=second@example.net"
BROKEN_WORD = "=?utf-8?q??=@example.com"
SECOND_HEADER = "=?utf-8?q?Hi=0D=0AX-Injected:_yes?="
HIDDEN_LINK = "=?utf-8?b?aHR0cHM6Ly9ldmlsLmV4YW1wbGUvcGF5?="

NOTIFICATION_HEADERS = [
    "Subject",
    "From",
    "To",
    "Date",
    "Message-ID",
    "MIME-Version",
    "Content-Type",
]
ACKNOWLEDGEMENT_HEADERS = [
    "Subject",
    "From",
    "To",
    "Reply-To",
    "Date",
    "Message-ID",
    "MIME-Version",
    "Content-Type",
]


@pytest.mark.parametrize(
    "address",
    [TWO_RECIPIENTS, BROKEN_WORD, "a,b@example.com"],
    ids=["decodes-to-two", "the-parser-cannot-read-it", "a-comma"],
)
async def test_an_address_that_is_not_one_plain_mailbox_gets_no_reply_and_the_owner_alone_is_told(
    fake: FakeOpenAI, server: FakeServer, address: str
) -> None:
    """Each of these passes the form's own check, which is n8n's and loose. None may
    reach a To header: the form would then have this system write to people the
    visitor chose, as many as fit in an address."""
    fake.verdict()

    outcome = await _handle(email=address)

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert [(delivery.sender, delivery.recipients) for delivery in server.delivered] == [
        (SENDER, (OWNER,))
    ]
    body = _text(server.sent[0])
    assert "Reply: The sender's address is not one a reply can safely be sent to." in body
    assert f"Sender Email: {address.lower()}" in body, "shown to the owner as it was typed"


@pytest.mark.parametrize(
    "subject", [SECOND_HEADER, HIDDEN_LINK], ids=["a-second-header", "a-hidden-link"]
)
async def test_a_subject_written_as_an_encoded_word_is_sent_as_the_odd_text_it_is(
    fake: FakeOpenAI, server: FakeServer, subject: str
) -> None:
    """Read from what the server was given, and not from the object: on the wire the
    first of these was once two header lines, in both emails."""
    fake.verdict()
    fake.paragraphs(FIRST)
    as_text = subject.replace("=?", "= ?")

    outcome = await _handle(subject=subject)

    assert outcome.acknowledged is True
    acknowledgement, notification = server.delivered
    assert acknowledgement.recipients == (VISITOR,)
    assert acknowledgement.subject == "Your message to Mihail Mihaylov"
    assert notification.subject.endswith(" - " + as_text)
    assert acknowledgement.header_names == ACKNOWLEDGEMENT_HEADERS
    assert notification.header_names == NOTIFICATION_HEADERS
    for delivery in server.delivered:
        assert b"evil.example" not in delivery.wire


@pytest.mark.parametrize(
    "subject",
    [
        "Claim your prize at https://example.com/pay",
        "Details at example.com/pay",
        "Write to someone@example.com",
        "Call 0800 123 4567 now",
        "Invoice" + chr(0x202E) + "fdp.exe",
    ],
    ids=["a-link", "a-bare-domain", "an-address", "a-phone-number", "reversed-text"],
)
async def test_whatever_the_subject_says_the_reply_goes_out_under_its_own(
    fake: FakeOpenAI, server: FakeServer, subject: str
) -> None:
    """The reply's subject is fixed, so what a visitor typed there reaches a visitor
    in no form at all. The owner's notification still carries it, made header-safe."""
    fake.verdict()
    fake.paragraphs(FIRST)

    outcome = await _handle(subject=subject)

    assert outcome.acknowledged is True
    acknowledgement, notification = server.delivered
    assert acknowledgement.subject == "Your message to Mihail Mihaylov"
    assert subject not in _text(server.sent[0])
    assert subject not in _html(server.sent[0])
    # Spaces normalised: a subject outside ASCII goes on the wire as encoded words, and
    # reading it back can show one more space beside them than was written.
    assert " ".join(notification.subject.split()).endswith(" - " + emails.header_text(subject))


@pytest.mark.parametrize(
    ("subject", "sent_as"),
    [
        ("Quote" + chr(0x85) + "today", "Quote today"),
        ("A Node.js developer, e.g. for 2.0", "A Node.js developer, e.g. for 2.0"),
    ],
    ids=["a-line-break-of-the-wide-kind", "dots-that-are-not-a-domain"],
)
async def test_an_honest_subject_with_something_odd_in_it_is_still_answered(
    fake: FakeOpenAI, server: FakeServer, subject: str, sent_as: str
) -> None:
    """U+0085 is what a mis-decoded ellipsis becomes. The rules' cleaning keeps it,
    because JavaScript does not count it as white space, and the mail library
    refuses it in a header, because Python counts it as a line break. Between the
    two, such a subject once meant the owner's notification could not be built."""
    fake.verdict()
    fake.paragraphs(FIRST)

    outcome = await _handle(subject=subject)

    assert outcome.acknowledged is True
    acknowledgement, notification = server.delivered
    assert acknowledgement.subject == "Your message to Mihail Mihaylov"
    assert notification.subject.endswith("[AUTO-REPLIED] - " + sent_as)


# --- how often a reply may go out -------------------------------------------------------------


async def test_one_inbox_is_sent_one_automatic_reply_a_day(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form takes any address, so the same person could be written to again and
    again. The second message from one inbox, here written another way, reaches the
    owner without a reply, and no reply is written for it: the only model asked the
    second time is the classifier."""
    recorder = LogRecorder()
    monkeypatch.setattr(service, "log", recorder)
    count = Replies()
    fake.verdict()
    fake.paragraphs(FIRST)
    fake.verdict()

    # An underscore, which Gmail does not allow, keeps these from being anybody's.
    first = await _handle(count, email="no_suchperson@gmail.com")
    second = await _handle(count, email="No_Such.Person+again@googlemail.com")

    assert (first.acknowledged, second.acknowledged) == (True, False)
    assert len(fake.requests) == 3, "two verdicts and one reply"
    assert [delivery.recipients for delivery in server.delivered] == [
        ("no_suchperson@gmail.com",),
        (OWNER,),
        (OWNER,),
    ]
    assert "Reply: This address was sent an automatic reply in the last 24 hours." in _text(
        server.sent[2]
    )
    [limited] = [line for line in recorder.lines if line["event"] == "contact_reply_limited"]
    assert limited["limit"] == "recipient"
    assert not recorder.holds("no_such")
    assert not recorder.holds("person")


async def test_the_whole_service_has_a_daily_cap_on_replies(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTACT_MAX_REPLIES_PER_DAY", "1")
    get_settings.cache_clear()
    count = Replies()
    fake.verdict()
    fake.paragraphs(FIRST)
    fake.verdict()

    first = await _handle(count, email="first@example.com")
    second = await _handle(count, email="second@example.com")

    assert (first.acknowledged, second.acknowledged) == (True, False)
    assert len(fake.requests) == 3, "the second reply was never written"
    assert (
        "Reply: Today's automatic replies have all been sent (CONTACT_MAX_REPLIES_PER_DAY)."
        in _text(server.sent[-1])
    )


@pytest.mark.usefixtures("server")
async def test_a_message_refused_a_reply_for_another_reason_uses_up_none_of_the_day(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count is asked only once the classifier and the rules have allowed a reply.
    A message they refuse it to leaves the day's replies for the messages that may
    have one."""
    monkeypatch.setenv("CONTACT_MAX_REPLIES_PER_DAY", "1")
    get_settings.cache_clear()
    count = Replies()
    fake.verdict(should_auto_reply=False)
    fake.verdict()
    fake.paragraphs(FIRST)

    refused = await _handle(count, email="first@example.com")
    answered = await _handle(count, email="second@example.com")

    assert (refused.acknowledged, answered.acknowledged) == (False, True)


async def test_a_cap_of_nought_switches_the_replies_off(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTACT_MAX_REPLIES_PER_DAY", "0")
    get_settings.cache_clear()
    fake.verdict()

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]
    assert "Reply: Automatic replies are switched off (CONTACT_MAX_REPLIES_PER_DAY is 0)." in _text(
        server.sent[0]
    )
    assert len(fake.requests) == 1


async def test_a_reply_counts_once_it_is_decided_on_even_if_it_is_then_held_back(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """Counted before it is written, and still counted when its checks then refuse
    it: the way to be wrong about a reply to a stranger is with fewer emails."""
    count = Replies()
    fake.verdict()
    fake.paragraphs("See https://example.com/pay to confirm.")

    outcome = await _handle(count)

    assert outcome.acknowledged is False
    assert [delivery.recipients for delivery in server.delivered] == [(OWNER,)]
    assert count.take(VISITOR, 10) is not None, "the address was counted all the same"


@pytest.mark.parametrize(
    ("paragraphs", "fault"),
    [
        (("See https://example.com/pay to confirm your account.",), "a link or an address"),
        (("Write to someone@example.com for the details.",), "a link or an address"),
        (("Thank you." + chr(27) + "[2J",), "not plain text"),
        (("word " * 500,), "too long"),
        (("", "   "), "empty"),
    ],
    ids=["a-link", "an-address", "an-escape-sequence", "too-long", "empty"],
)
async def test_a_reply_the_checks_refuse_is_never_sent(
    fake: FakeOpenAI, server: FakeServer, paragraphs: tuple[str, ...], fault: str
) -> None:
    """A reply is a model's output on its way to an address anyone could have typed
    into the form. What that can carry is bounded here, and the owner is told that a
    reply was written and held back, and why."""
    fake.verdict()
    fake.paragraphs(*paragraphs)

    outcome = await _handle()

    assert outcome.acknowledged is False
    [notification] = server.sent
    assert notification["To"] == OWNER, "nothing went to the visitor"
    assert f"Reply: The reply the model wrote was not sent ({fault})." in _text(notification)


async def test_an_acknowledgement_that_cannot_be_sent_is_reported_and_the_message_still_arrives(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.verdict()
    fake.paragraphs(FIRST)
    sent: list[tuple[str, str]] = []

    def send(box: mail.Mailbox, *, subject: str, **parts: object) -> None:
        assert parts["body"]
        if box.to == VISITOR:
            raise MailError("The email could not be sent (SMTPRecipientsRefused, code 550).")
        sent.append((box.to, subject))

    monkeypatch.setattr(mail, "send", send)

    outcome = await _handle()

    assert (outcome.status, outcome.acknowledged) == ("accepted", False)
    assert sent == [
        (OWNER, f"mihaylov.io - VALID Contact Form Submission [NO AUTO-REPLY] - {SUBJECT}")
    ]


async def test_a_notification_that_cannot_be_sent_fails_the_whole_submission(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """The one thing that has to happen did not, so the caller is told: the API turns
    this into an error and the form says the message was not sent."""
    server.fail_send = ConnectionResetError(104, "Connection reset by peer")

    with pytest.raises(MailError, match="could not be sent"):
        await _handle(reason="sales")

    assert fake.requests == []


async def test_a_refused_login_is_a_configuration_error(server: FakeServer) -> None:
    import smtplib  # ruff: ignore[import-outside-top-level] -- used by this one test

    server.fail_login = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password")

    with pytest.raises(ConfigError, match="rejected the login"):
        await _handle(reason="sales")


@pytest.mark.parametrize(
    ("unset", "named"),
    [("CONTACT_TO_EMAIL", "CONTACT_TO_EMAIL"), ("SMTP_APP_PASSWORD", "SMTP_APP_PASSWORD")],
)
async def test_without_its_mail_settings_the_form_cannot_take_a_message(
    fake: FakeOpenAI,
    server: FakeServer,
    monkeypatch: pytest.MonkeyPatch,
    unset: str,
    named: str,
) -> None:
    """Refused before any model is asked: a message nobody can be told about is not
    worth paying to classify. The error names the setting, for the log; the API
    turns it into a sentence that names nothing."""
    monkeypatch.delenv(unset)
    get_settings.cache_clear()

    with pytest.raises(MailError, match=f"Not set: {named}"):
        await _handle()

    assert (fake.requests, server.steps) == ([], [])


# --- what is sent is text, whatever was typed ---------------------------------------------


async def test_nothing_a_visitor_or_a_model_wrote_is_sent_as_markup(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """The workflow pasted the message into an HTML template as typed, and asked the
    model for HTML. Here both are escaped on the way into markup."""
    fake.verdict(explanation='Mentions "5 < 6" & nothing else.')
    fake.paragraphs("Thanks <b>very</b> much & see you.")

    await _handle(subject="5 < 6 & more", message='Is 5 < 6 & "true" here')

    acknowledgement, notification = server.sent
    assert "<p>Thanks &lt;b&gt;very&lt;/b&gt; much &amp; see you.</p>" in _html(acknowledgement)
    assert "<b>very</b>" not in _html(acknowledgement)
    page = _html(notification)
    assert "<p><strong>Message:</strong> Is 5 &lt; 6 &amp; &quot;true&quot; here</p>" in page
    assert "Mentions &quot;5 &lt; 6&quot; &amp; nothing else." in page
    # The text version says the same with nothing escaped.
    assert 'Message: Is 5 < 6 & "true" here' in _text(notification)


# --- nothing typed is logged ---------------------------------------------------------------


async def test_no_log_line_holds_what_was_typed_or_what_a_model_wrote(
    fake: FakeOpenAI, server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One line says what happened, in counts, flags and the rules' fixed wording.
    The address, the subject, the message, the model's explanation and its reply are
    in the emails and nowhere else."""
    recorder = LogRecorder()
    for module in (service, responses):
        monkeypatch.setattr(module, "log", recorder)
    fake.verdict(explanation=EXPLANATION)
    fake.paragraphs(FIRST, SECOND)

    await _handle(message=MESSAGE + " seo")

    assert len(server.sent) == 2
    [handled] = [line for line in recorder.lines if line["event"] == "contact_handled"]
    assert handled["status"] == "accepted"
    assert handled["acknowledged"] is True
    assert handled["rule_hits"] == ["Spam keyword hits: 1"]
    assert (handled["submitted_reason"], handled["detected_reason"]) == ("project", "project")
    assert handled["cost_usd"] != "0"
    assert sum(line["event"] == "llm_call" for line in recorder.lines) == 2
    for private in (VISITOR, SUBJECT, MESSAGE, EXPLANATION, FIRST, SECOND, OWNER, SENDER):
        assert not recorder.holds(private), private


async def test_a_reason_the_form_does_not_offer_is_not_logged(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid reason is one of five fixed words. An invalid one is whatever was
    typed, so it is left out of the line."""
    recorder = LogRecorder()
    monkeypatch.setattr(service, "log", recorder)

    await _handle(reason="please call me on my mobile")

    assert len(server.sent) == 1
    [handled] = recorder.lines
    assert handled["submitted_reason"] is None
    assert not recorder.holds("mobile")


# --- the two halves --------------------------------------------------------------------------


async def test_prepare_decides_everything_and_sends_nothing(
    fake: FakeOpenAI, server: FakeServer
) -> None:
    """What the terminal command uses, to show what would be sent."""
    fake.verdict()
    fake.paragraphs(FIRST)

    prepared = await service.prepare(
        email=VISITOR, subject=SUBJECT, message=MESSAGE, reason="project"
    )

    assert server.steps == []
    assert prepared.status == "accepted"
    assert prepared.acknowledgement is not None
    assert prepared.no_reply is None
    assert FIRST in prepared.acknowledgement.text
    as_sent = service.notification(prepared, sent=True)
    not_sent = service.notification(
        prepared, sent=False, failure="The reply was written and could not be sent."
    )
    assert "[AUTO-REPLIED]" in as_sent.subject
    assert "[NO AUTO-REPLY]" in not_sent.subject
    assert "Reply: The reply was written and could not be sent." in not_sent.text


def test_there_is_a_brief_for_each_reason_the_form_offers_and_no_other() -> None:
    """The classifier answers with one of the rules' five reasons, and the reply is
    asked for with the brief of that name. A sixth reason without a brief would be
    a KeyError on the first message of that kind."""
    assert set(CONTACT_BRIEFS) == rules.REASONS
