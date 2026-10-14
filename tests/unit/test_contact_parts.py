"""The contact flow's smaller parts, each by itself: what the models are shown, what
their answers may be, the emails' shape, and the desk that counts and keeps hold.

The flow that joins them is tested in test_contact_service.py.
"""

import asyncio
import sys
from decimal import Decimal
from email.message import EmailMessage

import pytest
from log_recorder import LogRecorder
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.assistant.prompts.loader import CONTACT_BRIEFS, CONTACT_CONTEXT
from portfolio_ai.contact import classifier, desk, emails, reply, rules
from portfolio_ai.contact.classifier import Verdict
from portfolio_ai.contact.desk import Desk
from portfolio_ai.contact.rules import Checked
from portfolio_ai.contact.service import Outcome
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

MESSAGE = "We track stock in spreadsheets and would like a small web tool for our team."

# Made, not typed: a source file should hold nothing a reviewer cannot see.
NB_HYPHEN = chr(0x2011)
EN_DASH = chr(0x2013)
# 020 7946 0958 in full-width digits, U+FF10 being a full-width nought.
FULL_WIDTH_NUMBER = " ".join(
    "".join(chr(0xFF10 + int(digit)) for digit in group) for group in ("020", "7946", "0958")
)


def _checked(**fields: str) -> Checked:
    given = {
        "email": "visitor@example.com",
        "subject": "A tool for our warehouse",
        "message": MESSAGE,
        "reason": "project",
        **fields,
    }
    return rules.check(**given)


def _verdict(**fields: object) -> Verdict:
    given: dict[str, object] = {
        "is_spam": False,
        "spam_confidence": 0.05,
        "detected_reason": "project",
        "reason_confidence": 0.9,
        "should_auto_reply": True,
        "explanation": "A clear project inquiry.",
        **fields,
    }
    return Verdict(**given)  # type: ignore[arg-type]


def _no_usage() -> CallUsage:
    return CallUsage(
        step="contact_classify",
        model="a-model",
        prompt=None,
        input_tokens=0,
        cached_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        latency_ms=0,
        cost=Decimal(0),
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


# --- what the classifier is shown, and what it may answer ---------------------------


def test_the_classifier_is_shown_the_submission_as_the_workflow_showed_it() -> None:
    """The text n8n built from its expressions, character for character: a boolean in
    lower case, as JavaScript prints one, and the rule hits as JSON with nothing
    after the commas, as JSON.stringify writes it."""
    checked = _checked(reason="job", message="We have an seo role, interested? Call me.")

    assert classifier.brief(checked) == (
        "Classify this contact form submission.\n"
        "\n"
        "Submission:\n"
        "- email: visitor@example.com\n"
        "- subject: A tool for our warehouse\n"
        "- message: We have an seo role, interested? Call me.\n"
        "- submitted_reason: job\n"
        "\n"
        "Rule-based precheck results:\n"
        "- spam_score: 3\n"
        "- suspicious: true\n"
        '- rule_hits: ["Spam keyword hits: 1","Reason/message mismatch"]\n'
        "\n"
        "Return the JSON classification object."
    )


async def test_a_verdict_is_tidied_before_anything_reads_it(fake: FakeOpenAI) -> None:
    """A model's output, treated as input: confidences kept between 0 and 1 whatever
    came back, and the explanation, which goes into an email, one clipped line."""
    fake.verdict(
        spam_confidence=-0.2,
        reason_confidence=7,
        explanation="First line.\n\n  Second   line. " + "x" * 600,
    )

    classified = await classifier.classify(_checked())

    verdict = classified.verdict
    assert verdict is not None
    assert (verdict.spam_confidence, verdict.reason_confidence) == (0.0, 1.0)
    assert verdict.explanation.startswith("First line. Second line. xxx")
    # 300, written out: measured against the constant it would pass at any value.
    assert len(verdict.explanation) == 300
    assert classified.usage.cost > 0


async def test_an_explanation_is_made_of_characters_that_can_be_printed(fake: FakeOpenAI) -> None:
    """It is printed in a terminal by ``contact try`` and put in an email. An escape
    sequence clears a terminal, and a character that reverses the text after it
    makes an email say something else."""
    fake.verdict(explanation="Looks fine." + chr(27) + "[2J" + chr(27) + "[H" + chr(0x202E) + "!")

    classified = await classifier.classify(_checked())

    assert classified.verdict is not None
    assert classified.verdict.explanation == "Looks fine.[2J[H!"


@pytest.mark.parametrize("field", ["spam_confidence", "reason_confidence"])
async def test_a_confidence_that_is_not_a_number_is_no_verdict(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """Clamped, NaN would come out as nought: "certainly not spam", from a model that
    said no such thing. The API's schema should never send one, so the answer is put
    straight into the parsed result here."""
    said = {
        "is_spam": False,
        "spam_confidence": 0.05,
        "detected_reason": "project",
        "reason_confidence": 0.9,
        "should_auto_reply": True,
        "explanation": "A clear project inquiry.",
        field: float("nan"),
    }

    async def parse(**_: object) -> responses.Parsed[classifier.ContactVerdict]:
        await asyncio.sleep(0)
        return responses.Parsed(
            value=classifier.ContactVerdict.model_validate(said), usage=_no_usage()
        )

    monkeypatch.setattr(responses, "parse", parse)

    assert (await classifier.classify(_checked())).verdict is None


async def test_a_reason_the_form_does_not_have_is_not_a_verdict(fake: FakeOpenAI) -> None:
    """The schema allows the five reasons and no other, so an answer with a sixth
    does not fit, and there is no verdict rather than a wrong brief."""
    fake.verdict(detected_reason="sales")

    assert (await classifier.classify(_checked())).verdict is None


# --- what the reply writer is shown, and what it may answer ---------------------------


@pytest.mark.parametrize("reason", sorted(CONTACT_BRIEFS))
def test_each_brief_is_filled_with_the_submission(reason: str) -> None:
    checked = _checked()

    text = reply.brief(checked, reason)  # type: ignore[arg-type]

    assert "- Email: visitor@example.com\n" in text
    assert "- Subject: A tool for our warehouse\n" in text
    assert f"- Message: {MESSAGE}\n" in text
    assert "Instructions for this reply:\n- " in text
    assert "{" not in text, "no field was left unfilled"
    # Four briefs describe Mihail, in the knowledge base's words. "Other" never did.
    assert (CONTACT_CONTEXT.text in text) == (reason != "other")


def test_braces_in_a_message_are_just_braces() -> None:
    """The brief is a template and the message is a value put into it. A visitor who
    types a field's name, or half a brace, changes nothing and breaks nothing."""
    odd = "My config is {email} and {0} and {context!r} and an open { brace"
    checked = _checked(message=odd)

    text = reply.brief(checked, "project")

    assert f"- Message: {odd}\n" in text
    assert "- Email: visitor@example.com\n" in text


@pytest.mark.parametrize(
    ("paragraphs", "fault"),
    [
        (("Thank you for your message.", "I will be in touch."), None),
        ((), "empty"),
        # The numbers written out, on each side of each limit: measured against the
        # constants, these would pass whatever the constants were.
        (("one",) * 6, "too long"),
        (("one",) * 5, None),
        (("x" * 2001,), "too long"),
        (("x" * 2000,), None),
        (("Thank you." + chr(0),), "not plain text"),
        (("Thank you." + chr(0x202E) + "uoy",), "not plain text"),
        (("See http://example.com for more.",), "a link or an address"),
        (("See WWW.example.com for more.",), "a link or an address"),
        (("Write to someone@example.com.",), "a link or an address"),
        # No scheme and no "www": a mail program makes a link of this by itself.
        (("Please confirm your details at example.com/pay today.",), "a link or an address"),
        (("Our shop is at Example.ORG, do visit.",), "a link or an address"),
        (("It is hosted on notmihaylov.io these days.",), "a link or an address"),
        (("We use Make.com for this.",), "a link or an address"),
        (("Thank you for writing in through mihaylov.io today.",), None),
        (("I work with Node.js, Vue.js and e.g. version 2.0 of the tool.",), None),
        # Nine digits or more is a phone number; eight is not, which lets a date through.
        (("Call us on +44 20 7946 0958 today.",), "a phone number"),
        (("Ring (555) 010-0199 for the details.",), "a phone number"),
        (("Our number is 123 456 789.",), "a phone number"),
        (("Our number is 12 345 678.",), None),
        (("We could start on 2026-11-02, say.",), None),
        (("Keep it to 80-160 words, in 2 or 3 paragraphs.",), None),
        # The ways a model writes a number besides the ASCII hyphen.
        ((f"Please call us on 020{NB_HYPHEN}7946{NB_HYPHEN}0958 to discuss.",), "a phone number"),
        ((f"Please call us on 020{EN_DASH}7946{EN_DASH}0958 to discuss.",), "a phone number"),
        (("Please call us on 020/7946/0958 to discuss.",), "a phone number"),
        ((f"Please call us on {FULL_WIDTH_NUMBER} to discuss.",), "a phone number"),
        (("Tarifs et références à Zürich.",), None),
    ],
    ids=[
        "fine",
        "nothing",
        "six-paragraphs",
        "five-paragraphs",
        "2001-characters",
        "2000-characters",
        "nul",
        "reversed-text",
        "a-link",
        "www",
        "an-address",
        "a-bare-domain-with-a-path",
        "a-bare-domain-in-capitals",
        "a-domain-that-ends-like-the-sites",
        "a-product-named-like-a-domain",
        "the-sites-own-name",
        "names-with-dots-that-are-not-domains",
        "an-international-number",
        "a-number-in-brackets",
        "nine-digits",
        "eight-digits",
        "an-iso-date",
        "a-range",
        "non-breaking-hyphens",
        "en-dashes",
        "slashes",
        "full-width-digits",
        "not-ascii",
    ],
)
def test_a_reply_is_checked_like_input(paragraphs: tuple[str, ...], fault: str | None) -> None:
    assert reply.fault(paragraphs) == fault


async def test_a_reply_is_flattened_and_its_empty_paragraphs_dropped(fake: FakeOpenAI) -> None:
    fake.paragraphs("  Thank you\nfor   your message. ", "", "   ", "I will be in touch.")

    drafted = await reply.draft(_checked(), "project")

    assert drafted.paragraphs == ("Thank you for your message.", "I will be in touch.")
    assert drafted.fault is None


async def test_a_reply_that_does_not_fit_the_shape_is_unusable(fake: FakeOpenAI) -> None:
    fake.unparseable()

    drafted = await reply.draft(_checked(), "project")

    assert (drafted.paragraphs, drafted.fault) == (None, "unusable")


# --- the emails ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "safe"),
    [
        ("visitor@example.com", True),
        ("first.last+tag@mail.example.co.uk", True),
        # 254 characters and 255, written out.
        ("x" * 242 + "@example.com", True),
        ("x" * 243 + "@example.com", False),
        # An encoded word: one mailbox to look at, two by the time it is a header.
        ("=?utf-8?q?first=40example.org=2C?=second@example.net", False),
        ("=?utf-8?q??=@example.com", False),
        ("a=?b@example.com", False),
        ("a,b@example.com", False),
        ("a@example.com,b@example.com", False),
        ("visitor@example", False),
        ("visitor@-example.com", False),
        ('"quoted name"@example.com', False),
        ("Visitor <visitor@example.com>", False),
        ("büro@example.com", False),
        ("visitor@example.com\nBcc: other@example.com", False),
        ("x" * 250 + "@example.com", False),
        ("", False),
    ],
)
def test_an_address_is_safe_to_reply_to_only_when_it_is_one_plain_mailbox(
    address: str, safe: bool
) -> None:
    """The form's own check is n8n's, kept as it was, and lets a comma through. A
    mail header reads "a,b@example.com" as two recipients, so this stricter test is
    asked before a typed address is ever put in one."""
    assert emails.safe_recipient(address) is safe


def test_an_acknowledgement_is_the_models_paragraphs_inside_fixed_words() -> None:
    """Fixed, the subject included: the paragraphs are the only part a model wrote,
    and nothing in it is anything a visitor typed."""
    email = emails.acknowledgement(["First.", "Second."])

    assert email.subject == "Your message to Mihail Mihaylov"
    assert email.text == (
        "Hello,\n\nFirst.\n\nSecond.\n\nKind Regards,\nMihail\n\n"
        f"{emails.AUTOMATIC}\n{emails.NOT_YOU}\n"
    )
    assert email.html.startswith("<p>Hello,</p>\n<p>First.</p>\n<p>Second.</p>\n")
    assert "<p>Kind Regards,<br>Mihail</p>" in email.html
    assert f"{emails.AUTOMATIC}<br>{emails.NOT_YOU}</p>" in email.html
    assert "automatic acknowledgement" in emails.AUTOMATIC
    assert "will reply personally" in emails.AUTOMATIC
    assert emails.NOT_YOU == (
        "You are receiving this because this address was entered in the contact form on "
        "mihaylov.io. If that was not you, you can ignore this email."
    )


def test_a_long_subject_is_clipped_and_a_missing_one_is_said_to_be_missing() -> None:
    long = emails.blocked_by_rules(_checked(subject="s" * 400, reason="sales"))
    none = emails.blocked_by_rules(_checked(subject=" ", reason="sales"))

    # 200, written out: measured against the constant, the test would pass at any value.
    assert long.subject.endswith(" - " + "s" * 200)
    assert none.subject == "mihaylov.io - BLOCKED Contact Form Submission [INVALID] - No Subject"


def test_a_subject_is_made_into_text_a_header_carries_as_it_is() -> None:
    """Two things a header would do to a visitor's words. A line break is refused,
    which would lose the owner's notification, and an encoded word is decoded, which
    would send a subject nobody read."""
    assert emails.header_text("Quote" + chr(0x85) + "today") == "Quote today"
    assert emails.header_text("=?utf-8?b?aHR0cHM6Ly9ldmlsLmV4YW1wbGUvcGF5?=") == (
        "= ?utf-8?b?aHR0cHM6Ly9ldmlsLmV4YW1wbGUvcGF5?="
    )
    assert emails.header_text("An ordinary subject, 3 = 3?") == "An ordinary subject, 3 = 3?"

    blocked = emails.blocked_by_rules(_checked(subject="Quote" + chr(0x85) + "today", reason="x"))
    assert blocked.subject.endswith(" - Quote today")


def test_no_character_a_visitor_can_type_keeps_a_subject_from_being_a_header() -> None:
    """Every character the mail library counts as a line break, found by asking the
    function it asks (``str.splitlines``) about each character there is. Each is put
    into a subject and the subject into a real header. Before ``header_text``, U+0085
    in a subject meant the notification could not be built at all."""
    breaks = [
        chr(point) for point in range(sys.maxunicode + 1) if len(f"a{chr(point)}b".splitlines()) > 1
    ]
    assert chr(0x85) in breaks
    assert chr(0x2028) in breaks

    for char in breaks:
        message = EmailMessage()
        message["Subject"] = emails.header_text(f"Quote{char}today")
        assert str(message["Subject"]) == "Quote today", hex(ord(char))


def test_line_breaks_in_a_value_become_tags_after_it_is_escaped_and_not_before() -> None:
    """The reply quoted in the owner's notification has line breaks and whatever a
    model wrote. Escaped first, the breaks become real tags and the model's angle
    brackets do not. The other way round, the tags would be escaped too."""
    reply_sent = emails.acknowledgement(["Is 5 <b>less</b> than 6?"])

    note = emails.accepted(_checked(), _verdict(), reply=reply_sent, no_reply=None)

    assert "Hello,<br><br>Is 5 &lt;b&gt;less&lt;/b&gt; than 6?<br><br>Kind Regards,<br>Mihail" in (
        note.html
    )
    assert "&lt;br&gt;" not in note.html


def test_each_kind_of_notification_says_which_it_is_in_its_subject() -> None:
    """In the workflow's own wording, so that a mail filter written for n8n's
    notifications still matches. "NOT CLASSIFIED" is the one that is new."""
    clean = _checked()
    spam = _checked(email="someone@mailinator.com", message="seo??")
    verdict = _verdict()
    reply_sent = emails.acknowledgement(["Thanks."])
    prefix = "mihaylov.io - "
    subject = " - A tool for our warehouse"

    assert [
        emails.accepted(clean, verdict, reply=reply_sent, no_reply=None).subject,
        emails.accepted(clean, verdict, reply=None, no_reply="Because.").subject,
        emails.unclassified(clean, why="Because.").subject,
        emails.blocked_by_rules(_checked(reason="sales")).subject,
        emails.blocked_by_rules(spam).subject,
        emails.blocked_by_ai(clean, _verdict(is_spam=True)).subject,
    ] == [
        f"{prefix}VALID Contact Form Submission [AUTO-REPLIED]{subject}",
        f"{prefix}VALID Contact Form Submission [NO AUTO-REPLY]{subject}",
        f"{prefix}VALID Contact Form Submission [NOT CLASSIFIED]{subject}",
        f"{prefix}BLOCKED Contact Form Submission [INVALID]{subject}",
        f"{prefix}BLOCKED Contact Form Submission [RULE_SPAM]{subject}",
        f"{prefix}BLOCKED Contact Form Submission [AI_SPAM]{subject}",
    ]


def test_a_notification_is_the_same_thing_twice_as_text_and_as_markup() -> None:
    email = emails.blocked_by_ai(_checked(), _verdict(is_spam=True, explanation="Junk & more."))

    assert email.text.startswith("Blocked Contact Form Submission\n\nStatus: Blocked by AI")
    assert "\nRequest Details\n---------------\nSender Email: visitor@example.com\n" in email.text
    assert email.html.startswith("<h2>Blocked Contact Form Submission</h2>")
    assert "<h3>Request Details</h3>" in email.html
    assert "<p><strong>Why blocked:</strong> Junk &amp; more.</p>" in email.html


# --- the desk: the hour's count ------------------------------------------------------------


def test_the_desk_admits_so_many_an_hour_and_says_how_long_to_wait() -> None:
    counter = Desk()

    assert counter.admit(2, now=1000.0) is None
    assert counter.admit(2, now=1600.0) is None
    # Full. The first leaves an hour after it came: in 3,000 seconds from here.
    assert counter.admit(2, now=1600.0) == 3000
    assert counter.admit(2, now=4599.5) == 1, "rounded up: never told to come back too soon"
    # An hour after the first, there is room for one more, and then none.
    assert counter.admit(2, now=4600.0) is None
    assert counter.admit(2, now=4600.0) == 600


def test_a_refusal_is_not_counted_as_an_arrival() -> None:
    """Or a flood would keep the door shut on itself for ever: each refused attempt
    would push the hour's end further away."""
    counter = Desk()
    assert counter.admit(1, now=0.0) is None

    for moment in (10.0, 20.0, 3599.0):
        assert counter.admit(1, now=moment) is not None

    assert counter.admit(1, now=3600.0) is None


# --- the desk: work in hand ------------------------------------------------------------------

OUTCOME = Outcome(status="accepted", acknowledged=True, spam_score=0, cost=0)  # type: ignore[arg-type]


async def test_work_in_hand_is_kept_hold_of_until_it_is_done() -> None:
    release = asyncio.Event()

    async def work() -> Outcome:
        await release.wait()
        return OUTCOME

    counter = Desk()
    task = counter.start(work())
    await asyncio.sleep(0)

    assert counter.in_hand == 1
    release.set()
    assert await task is OUTCOME
    await asyncio.sleep(0)  # the done-callback runs on the loop's next pass
    assert counter.in_hand == 0


async def test_work_goes_on_when_whoever_was_waiting_for_it_is_cancelled() -> None:
    """What the endpoint relies on. The request awaits the task through
    ``asyncio.shield``; cancelling the request cancels the waiting, and the task,
    which may be between the visitor's email and the owner's, runs to its end."""
    release = asyncio.Event()
    finished: list[str] = []

    async def work() -> Outcome:
        await release.wait()
        finished.append("done")
        return OUTCOME

    counter = Desk()
    task = counter.start(work())

    async def request() -> None:
        await asyncio.shield(task)

    waiting = asyncio.create_task(request())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert not task.done(), "the work was not cancelled with its waiter"
    release.set()
    await counter.drain(grace_seconds=5)
    assert finished == ["done"]


async def test_a_failure_nobody_was_waiting_for_is_logged_by_its_kind_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = LogRecorder()
    monkeypatch.setattr(desk, "log", recorder)

    async def work() -> Outcome:  # ruff: ignore[unused-async]
        raise RuntimeError("the visitor wrote: please call me")

    counter = Desk()
    task = counter.start(work())
    await asyncio.wait([task])
    await asyncio.sleep(0)

    assert recorder.lines == [{"event": "contact_failed", "error_type": "RuntimeError"}]
    assert not recorder.holds("please call me")


async def test_at_shutdown_work_in_hand_is_waited_for_and_then_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = LogRecorder()
    monkeypatch.setattr(desk, "log", recorder)
    never = asyncio.Event()

    async def stuck() -> Outcome:
        await never.wait()
        return OUTCOME

    counter = Desk()
    task = counter.start(stuck())
    await asyncio.sleep(0)

    await counter.drain(grace_seconds=0.01)

    assert task.cancelled()
    assert [line["event"] for line in recorder.lines] == [
        "contact_draining",
        "contact_cancelled_at_shutdown",
    ]
