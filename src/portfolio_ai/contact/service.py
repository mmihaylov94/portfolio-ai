"""One contact form message, from the form's four fields to the emails it becomes.

The flow, in the order of the n8n workflow it replaces::

    rules.check ----------- invalid, or spam ------------> blocked notification
        |
    classifier.classify --- no verdict to be had ---------> notification, not classified
        |               +-- says spam --------------------> blocked notification
        |
    is an acknowledgement safe? -- no --+
        | yes                           |
    reply.draft, then send it           |
        |                               |
        +-------------------------------+-----------------> notification, accepted

Two halves, kept apart. :func:`prepare` thinks: it runs the rules and asks the two
models, and sends nothing. :func:`handle` is ``prepare`` and then the sending. The
terminal command (``python -m portfolio_ai.contact try``) uses the first half alone,
to show what would be sent.

**The owner's notification is the one thing that must happen.** Everything before
it may fail without losing the message:

- OpenAI cannot be reached, takes too long, or its answer is unusable: the message
  is passed on marked "not classified", with no acknowledgement.
- The acknowledgement cannot be written in time, is refused by its checks, or cannot
  be sent: the message is passed on, and the notification says why no reply went out.

"May fail" means in any way at all. Each of those three steps is wrapped in
``except Exception``, which is wider than this project usually writes and is meant:
the list of errors somebody thought of is never the list that happens. OpenAI can
answer 200 with a page of HTML, and the client then fails with an ``AttributeError``;
the mail library's header parser can fall over with an ``IndexError``. A courtesy
that fails must not stop the one email that matters. ``asyncio.CancelledError`` is
not an ``Exception``, so shutdown still stops a step where it stands.
- The notification itself cannot be sent: :func:`handle` raises, the API answers
  with an error, and the visitor is told the message did not go. The n8n workflow
  answered the form before it did anything, so there a failed run was silent.

**Nothing a visitor typed is logged or stored.** The one log line carries the
outcome, the rules' fixed wording, numbers and a cost. The message exists in the
request, in the two calls to OpenAI, and in the emails.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import structlog

from portfolio_ai import mail
from portfolio_ai.config import get_settings
from portfolio_ai.contact import classifier, emails, reply, rules
from portfolio_ai.contact.classifier import Verdict
from portfolio_ai.contact.emails import Email
from portfolio_ai.contact.replies import Replies
from portfolio_ai.contact.rules import Checked
from portfolio_ai.exceptions import MailError
from portfolio_ai.mail import Mailbox

log = structlog.get_logger(__name__)

# An acknowledgement goes out only when the classifier is at least this sure what the
# message is about, and at most this sure it is spam. The workflow's two numbers.
MIN_REASON_CONFIDENCE = 0.6
MAX_SPAM_CONFIDENCE = 0.3

# How long each step may take. Left alone, the OpenAI client waits
# OPENAI_TIMEOUT_SECONDS for an attempt and makes several, which is minutes, and
# smtplib is given half a minute for each step of its conversation. Here a visitor
# is watching a form, and the site's API gives up on this service after 35 seconds
# (contactMs in the portfolio's api/src/config.js). The four below add up to 30. A
# model that has not answered by its deadline is treated as one that could not be
# asked, and an email that has not gone by its own as one that could not be sent.
CLASSIFY_SECONDS = 8.0
REPLY_SECONDS = 10.0
SEND_SECONDS = 6.0

type Status = Literal["invalid", "rule_spam", "ai_spam", "not_classified", "accepted"]


@dataclass(frozen=True)
class Prepared:
    """What was decided about one submission, before anything is sent."""

    checked: Checked
    status: Status
    # None when the rules blocked it, or when no verdict could be had.
    verdict: Verdict | None
    # The acknowledgement to send, or None, in which case no_reply says why for an
    # accepted message. Both None for one that was blocked or not classified.
    acknowledgement: Email | None
    no_reply: str | None
    # What the two calls cost. Nought for a message the rules blocked.
    cost: Decimal


@dataclass(frozen=True)
class Outcome:
    """What happened to one submission. Counts and flags, and nothing typed."""

    status: Status
    acknowledged: bool
    spam_score: int
    cost: Decimal


def _why_no_reply(checked: Checked, verdict: Verdict) -> str | None:
    """Why an accepted message gets no acknowledgement, or None when it may have one.

    The workflow's four conditions, then one of this project's own: the address has
    to be one a mail header can carry as a single recipient. How often a reply may go
    out is asked afterwards, of ``replies``, because asking counts it.
    """
    # Each condition with the sentence the owner is shown when it holds, in the order
    # they are asked.
    refusals = (
        (
            not verdict.should_auto_reply,
            "The classifier advised against an automatic reply.",
        ),
        (
            verdict.reason_confidence < MIN_REASON_CONFIDENCE,
            "The classifier was not sure enough what the message is about.",
        ),
        (
            verdict.spam_confidence > MAX_SPAM_CONFIDENCE,
            "The classifier was not sure enough that the message is not spam.",
        ),
        (
            checked.suspicious,
            "The fixed rules found the message suspicious.",
        ),
        (
            not emails.safe_recipient(checked.email),
            "The sender's address is not one a reply can safely be sent to.",
        ),
    )
    # next() takes the first item a generator yields, or its second argument when
    # the generator yields nothing: the first refusal that holds, or None.
    return next((why for holds, why in refusals if holds), None)


async def _verdict(checked: Checked) -> tuple[Verdict | None, Decimal]:
    """The classifier's verdict and what asking cost. None when there is none to be had."""
    try:
        # asyncio.timeout is a deadline for whatever is awaited inside it. When it
        # passes, the call is cancelled where it stands, the client's retries with
        # it, and TimeoutError is raised here in its place.
        async with asyncio.timeout(CLASSIFY_SECONDS):
            classified = await classifier.classify(checked)
    except Exception as exc:
        # OpenAI is down or slow, the key was refused, or what came back was not a
        # response at all. Whatever it was, the message goes on. The kind of error
        # is logged and never its words, which can quote what was sent.
        log.warning("contact_classifier_failed", error_type=type(exc).__name__)
        return None, Decimal(0)
    return classified.verdict, classified.usage.cost


async def _acknowledgement(
    checked: Checked, verdict: Verdict, replies: Replies | None
) -> tuple[Email | None, str | None, Decimal]:
    """The reply to send, or why there is none, and what writing it cost.

    Exactly one of the first two is set. ``replies`` is the API's count of who has
    been written to; with None, nothing limits how often (``contact try``).
    """
    no_reply = _why_no_reply(checked, verdict)
    if no_reply is not None:
        return None, no_reply, Decimal(0)

    if replies is not None:
        # Asked before the reply is written, so that one which may not be sent costs
        # no model call. Asking counts the reply, and it stays counted if the reply
        # is then refused by its checks or fails to send: the way to be wrong here is
        # with fewer emails, not more.
        refusal = replies.take(checked.email, get_settings().contact_max_replies_per_day)
        if refusal is not None:
            # Which limit, and never the address.
            log.warning("contact_reply_limited", limit=refusal.limit)
            return None, refusal.why, Decimal(0)

    try:
        async with asyncio.timeout(REPLY_SECONDS):
            drafted = await reply.draft(checked, verdict.detected_reason)
    except Exception as exc:
        log.warning("contact_reply_failed", error_type=type(exc).__name__)
        return None, "The reply could not be written.", Decimal(0)

    if drafted.paragraphs is None:
        # The fault is a few fixed words (reply.fault), so it may be logged and shown.
        log.warning("contact_reply_unusable", fault=drafted.fault)
        why = f"The reply the model wrote was not sent ({drafted.fault})."
        return None, why, drafted.usage.cost
    return emails.acknowledgement(drafted.paragraphs), None, drafted.usage.cost


async def prepare(
    *, email: str, subject: str, message: str, reason: str, replies: Replies | None = None
) -> Prepared:
    """Run the rules and ask the models. Sends nothing, and never raises for OpenAI.

    Given ``replies``, a reply that one of its limits refuses is not written, and is
    counted when it is let through.
    """
    checked = rules.check(email=email, subject=subject, message=message, reason=reason)
    if checked.blocked:
        status: Status = "rule_spam" if checked.is_valid else "invalid"
        return Prepared(checked, status, None, None, None, Decimal(0))

    verdict, cost = await _verdict(checked)
    if verdict is None:
        return Prepared(checked, "not_classified", None, None, None, cost)
    if verdict.is_spam:
        return Prepared(checked, "ai_spam", verdict, None, None, cost)

    acknowledgement, no_reply, spent = await _acknowledgement(checked, verdict, replies)
    return Prepared(checked, "accepted", verdict, acknowledgement, no_reply, cost + spent)


def notification(prepared: Prepared, *, sent: bool, failure: str | None = None) -> Email:
    """The owner's email for what :func:`prepare` decided.

    ``sent`` is whether the acknowledgement actually went, which only the sender
    knows; ``failure`` is why it did not, when it was meant to.
    """
    checked, verdict = prepared.checked, prepared.verdict
    if prepared.status in {"invalid", "rule_spam"}:
        return emails.blocked_by_rules(checked)
    if verdict is None:
        return emails.unclassified(
            checked, why="The classifier could not be asked, or its answer could not be used."
        )
    if prepared.status == "ai_spam":
        return emails.blocked_by_ai(checked, verdict)
    return emails.accepted(
        checked,
        verdict,
        reply=prepared.acknowledgement if sent else None,
        no_reply=failure or prepared.no_reply,
    )


async def _send(box: Mailbox, email: Email, *, name: str, reply_to: str | None = None) -> None:
    """Send one email without holding up the server.

    ``smtplib`` blocks until the mail server has answered, and this runs inside the
    API, whose event loop is also writing chat answers. ``asyncio.to_thread`` runs
    the blocking function on a worker thread and lets this coroutine wait for it,
    with the loop free in between. The digest does not need it: it sends after its
    loop has finished (``mail.py`` has both halves of that story).

    Two limits, both ``SEND_SECONDS``. ``asyncio.timeout`` is how long this coroutine
    waits, and it raises ``TimeoutError``. It cannot stop the thread: a thread, once
    started, runs until its function returns. So the same number is handed to
    ``smtplib`` as its own timeout for each step, which is what makes the thread give
    up soon after. An email that timed out here may therefore still arrive.
    """
    async with asyncio.timeout(SEND_SECONDS):
        await asyncio.to_thread(
            mail.send,
            box,
            subject=email.subject,
            body=email.text,
            html=email.html,
            reply_to=reply_to,
            from_name=name,
            timeout=SEND_SECONDS,
        )


async def handle(
    *, email: str, subject: str, message: str, reason: str, replies: Replies
) -> Outcome:
    """Deal with one submission: decide, acknowledge if that is safe, notify the owner.

    ``replies`` is the app's count of automatic replies (``contact/replies.py``),
    which says whether this one may go out at all.

    Raises ``MailError`` when the mail settings are missing or the notification
    cannot be sent, or is not sent in time, and ``ConfigError`` when the mail server
    refuses the login. Both mean the owner was not told, as far as this knows, and
    the caller has to say so.
    """
    settings = get_settings()
    owner = mail.mailbox(settings.contact_to_email)
    if owner is None:
        missing = mail.missing_settings("CONTACT_TO_EMAIL", settings.contact_to_email)
        raise MailError(f"The contact form cannot send. Not set: {', '.join(missing)}.")

    prepared = await prepare(
        email=email, subject=subject, message=message, reason=reason, replies=replies
    )

    sent = False
    failure: str | None = None
    if prepared.acknowledgement is not None:
        # To the visitor, from the same account, with the owner's address as where an
        # answer goes. dataclasses.replace: a frozen dataclass cannot be changed, so
        # this is a copy of the mailbox with a different recipient.
        visitor = dataclasses.replace(owner, to=prepared.checked.email)
        try:
            await _send(visitor, prepared.acknowledgement, name=emails.FROM_NAME, reply_to=owner.to)
        except Exception as exc:
            # The owner still has to hear of the message; the notification says the
            # reply did not go. The kind of failure, never the server's words.
            log.warning("contact_acknowledgement_failed", error_type=type(exc).__name__)
            failure = (
                "The reply was written, and sending it did not finish in time."
                if isinstance(exc, TimeoutError)
                else "The reply was written and could not be sent."
            )
        else:
            sent = True

    try:
        await _send(
            owner, notification(prepared, sent=sent, failure=failure), name=emails.NOTIFIER_NAME
        )
    except TimeoutError as exc:
        # A mail error like any other, so that the caller hears 503 and not a bug.
        raise MailError("The email could not be sent (it took too long).") from exc

    outcome = Outcome(
        status=prepared.status,
        acknowledged=sent,
        spam_score=prepared.checked.spam_score,
        cost=prepared.cost,
    )
    verdict = prepared.verdict
    # Counts, flags and the rules' own fixed wording. Not the address, the subject,
    # the message, the model's explanation or its reply.
    log.info(
        "contact_handled",
        status=outcome.status,
        acknowledged=outcome.acknowledged,
        spam_score=outcome.spam_score,
        rule_hits=list(prepared.checked.rule_hits),
        submitted_reason=prepared.checked.reason if prepared.checked.is_valid else None,
        detected_reason=verdict.detected_reason if verdict else None,
        spam_confidence=verdict.spam_confidence if verdict else None,
        reason_confidence=verdict.reason_confidence if verdict else None,
        cost_usd=str(outcome.cost),
    )
    return outcome
