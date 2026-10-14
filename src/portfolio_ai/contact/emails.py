"""The emails a contact form message produces: one to the visitor, one to the owner.

Nothing here sends. Each function returns an :class:`Email`, a subject and the same
body twice, as plain text and as HTML, and ``service.py`` hands it to ``mail.send``.

**Every piece of HTML is built here, by code, from escaped text.** The workflow this
replaces pasted the visitor's message into an HTML template as it was typed, and
asked a model to write the reply as HTML. Here a visitor's words, a model's words
and a rule's wording all go through ``html.escape`` on the way into markup, so a
message of ``<script>`` arrives as the eight characters it is.

The owner's notification comes in four kinds, and the subject says which, in the
workflow's own wording so that mail filters made for it still match:

- ``VALID ... [AUTO-REPLIED]`` / ``[NO AUTO-REPLY]``: accepted
- ``VALID ... [NOT CLASSIFIED]``: accepted with no verdict, because the model
  could not be asked. New here; n8n lost such a message.
- ``BLOCKED ... [INVALID]`` / ``[RULE_SPAM]``: stopped by the fixed rules
- ``BLOCKED ... [AI_SPAM]``: stopped by the classifier
"""

import html
import re
from collections.abc import Sequence
from dataclasses import dataclass

from portfolio_ai.contact.classifier import Verdict
from portfolio_ai.contact.rules import Checked

# The name beside the sending address on an acknowledgement.
FROM_NAME = "Mihail Mihaylov"
# And on a notification, which the owner receives from his own form.
NOTIFIER_NAME = "mihaylov.io contact form"

# Under the signature of every acknowledgement. The reply above it reads as a
# personal one; this says what it is.
AUTOMATIC = (
    "This is an automatic acknowledgement. Mihail reads every message and will reply personally."
)
# And under that, for whoever never wrote: the form takes any address, so some of
# these go to people who did not ask for them.
NOT_YOU = (
    "You are receiving this because this address was entered in the contact form on "
    "mihaylov.io. If that was not you, you can ignore this email."
)
# The acknowledgement's subject. Fixed, and not the visitor's own: it heads an email
# sent in the owner's name to an address anybody could have typed, and a visitor's
# subject was the one part of it an attacker wrote word for word.
ACKNOWLEDGEMENT_SUBJECT = "Your message to Mihail Mihaylov"

# How much of a visitor's subject goes into a subject line.
SUBJECT_LENGTH = 200

# An address a reply can safely be sent to: one mailbox, in plain ASCII. The form's
# own check (rules.py, kept as n8n had it) lets through "a,b@example.com", which a
# mail header reads as two recipients. This is the stricter test, asked only before
# an address a visitor typed is put into a header.
_RECIPIENT = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)
_LONGEST_ADDRESS = 254


@dataclass(frozen=True)
class Email:
    subject: str
    text: str
    html: str


@dataclass(frozen=True)
class _Section:
    """A heading, or None for the lines above the first one, and labelled values."""

    title: str | None
    rows: tuple[tuple[str, str], ...]


def safe_recipient(address: str) -> bool:
    """Whether ``address`` can go into a To header and mean one mailbox.

    ``=?`` is refused although every character of it is allowed in an address: it
    opens an "encoded word", which the mail library decodes when it builds a header.
    ``=?utf-8?q?first=40example.org=2C?=second@example.net`` looks like one mailbox
    here and is two by the time it is sent. ``mail.py`` checks the built header as
    well; this keeps such an address from being tried at all.
    """
    return (
        len(address) <= _LONGEST_ADDRESS
        and "=?" not in address
        and _RECIPIENT.fullmatch(address) is not None
    )


def header_text(text: str) -> str:
    """What a visitor typed, made into text a mail header carries as it is.

    Two things would otherwise happen to it on the way into a subject line:

    - **A line break is refused**, and the library's idea of one is Python's
      ``str.splitlines``, which is wider than the cleaning the rules do: it includes
      U+0085, which a mis-decoded ellipsis turns into. Refused, the owner's
      notification would not be built and the message would be lost. So line breaks
      of every kind become spaces here.
    - **An encoded word is decoded.** ``=?utf-8?b?...?=`` in a subject is sent as
      whatever it decodes to, which nobody read: not the rules, not the classifier.
      A space after the ``=`` leaves it as the odd text it is.
    """
    return " ".join(text.splitlines()).replace("=?", "= ?")


def _page(heading: str, sections: Sequence[_Section]) -> tuple[str, str]:
    """One notification, as text and as HTML, from the same headings and rows."""
    text = [heading, ""]
    markup = [f"<h2>{html.escape(heading)}</h2>"]
    for section in sections:
        if section.title is not None:
            text.extend([section.title, "-" * len(section.title)])
            markup.append(f"<h3>{html.escape(section.title)}</h3>")
        for label, value in section.rows:
            text.append(f"{label}: {value}")
            # Escaped first, and only then are line breaks made into tags: the other
            # way round the tags would be escaped too.
            shown = html.escape(value).replace("\n", "<br>")
            markup.append(f"<p><strong>{html.escape(label)}:</strong> {shown}</p>")
        text.append("")
    return "\n".join(text), "\n".join(markup)


def _subject(kind: str, flag: str, checked: Checked) -> str:
    return (
        f"mihaylov.io - {kind} Contact Form Submission [{flag}] - "
        f"{header_text(checked.subject)[:SUBJECT_LENGTH] or 'No Subject'}"
    )


def _request(checked: Checked, verdict: Verdict | None = None) -> _Section:
    rows = [
        ("Sender Email", checked.email),
        ("Submitted Reason", checked.reason),
        *([("Detected Reason", verdict.detected_reason)] if verdict else []),
        ("Subject", checked.subject),
        ("Message", checked.message),
    ]
    return _Section("Request Details", tuple(rows))


def _rules(checked: Checked) -> _Section:
    hits = "\n".join(f"- {hit}" for hit in checked.rule_hits) or "None"
    return _Section(
        "Rule-Based Validation",
        (
            ("Is Valid", str(checked.is_valid).lower()),
            ("Is Rule Spam", str(checked.is_spam_rule).lower()),
            ("Suspicious", str(checked.suspicious).lower()),
            ("Spam Score", str(checked.spam_score)),
            ("Rule Hits", hits),
        ),
    )


def _classification(verdict: Verdict) -> _Section:
    return _Section(
        "AI Classification",
        (
            ("AI Marked as Spam", str(verdict.is_spam).lower()),
            ("Spam Confidence", f"{verdict.spam_confidence:g}"),
            ("Detected Reason", verdict.detected_reason),
            ("Reason Confidence", f"{verdict.reason_confidence:g}"),
            ("Should Auto-Reply", str(verdict.should_auto_reply).lower()),
            ("AI Explanation", verdict.explanation),
        ),
    )


def acknowledgement(paragraphs: Sequence[str]) -> Email:
    """The reply to the visitor: the model's paragraphs between a fixed greeting and
    a fixed signature, with the two lines underneath that say what it is.

    Every word of it but the paragraphs is fixed, the subject included. The workflow
    used the visitor's own subject; here no code puts anything a visitor typed into an
    email sent to a visitor. The model's paragraphs, written from the message, are
    the only part that can echo any of it, and ``reply.fault`` and the classifier
    bound what they can say.
    """
    footer = f"{AUTOMATIC}\n{NOT_YOU}"
    text = "\n\n".join(["Hello,", *paragraphs, "Kind Regards,\nMihail", footer]) + "\n"
    small = f"{html.escape(AUTOMATIC)}<br>{html.escape(NOT_YOU)}"
    markup = "\n".join(
        [
            "<p>Hello,</p>",
            *(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs),
            "<p>Kind Regards,<br>Mihail</p>",
            f'<p style="color: #666666; font-size: small;">{small}</p>',
        ]
    )
    return Email(subject=ACKNOWLEDGEMENT_SUBJECT, text=text, html=markup)


def accepted(
    checked: Checked, verdict: Verdict, *, reply: Email | None, no_reply: str | None
) -> Email:
    """The owner's notification of a message that was let through.

    ``reply`` is the acknowledgement that was sent, or None, in which case
    ``no_reply`` says in a sentence why none was.
    """
    sent = reply.text.strip() if reply else (no_reply or "No auto-reply was sent.")
    text, markup = _page(
        "Valid Contact Form Submission",
        [
            _Section(
                None,
                (("Status", "Accepted"), ("Auto-reply sent", "Yes" if reply else "No")),
            ),
            _request(checked, verdict),
            _rules(checked),
            _classification(verdict),
            _Section("Auto-Reply Sent", (("Reply", sent),)),
        ],
    )
    flag = "AUTO-REPLIED" if reply else "NO AUTO-REPLY"
    return Email(subject=_subject("VALID", flag, checked), text=text, html=markup)


def unclassified(checked: Checked, *, why: str) -> Email:
    """The owner's notification of a message no model could be asked about.

    It passed the fixed rules, so it is handed on as it is, with no verdict and no
    acknowledgement. ``why`` is a sentence of this project's own, never the error's.
    """
    text, markup = _page(
        "Valid Contact Form Submission",
        [
            _Section(
                None,
                (
                    ("Status", "Accepted, not classified"),
                    ("Why", why),
                    ("Auto-reply sent", "No"),
                ),
            ),
            _request(checked),
            _rules(checked),
        ],
    )
    return Email(subject=_subject("VALID", "NOT CLASSIFIED", checked), text=text, html=markup)


def blocked_by_rules(checked: Checked) -> Email:
    """The owner's notification of a message the fixed rules stopped."""
    invalid = not checked.is_valid
    text, markup = _page(
        "Blocked Contact Form Submission",
        [
            _Section(
                None,
                (
                    (
                        "Status",
                        "Invalid request" if invalid else "Blocked by rule-based spam checks",
                    ),
                    ("Why blocked", checked.block_reason or "No reason provided"),
                    ("Auto-reply sent", "No"),
                ),
            ),
            _request(checked),
            _rules(checked),
        ],
    )
    flag = "INVALID" if invalid else "RULE_SPAM"
    return Email(subject=_subject("BLOCKED", flag, checked), text=text, html=markup)


def blocked_by_ai(checked: Checked, verdict: Verdict) -> Email:
    """The owner's notification of a message the classifier called spam."""
    text, markup = _page(
        "Blocked Contact Form Submission",
        [
            _Section(
                None,
                (
                    ("Status", "Blocked by AI classifier"),
                    ("Why blocked", verdict.explanation or "No reason provided"),
                    ("Auto-reply sent", "No"),
                ),
            ),
            _request(checked),
            _rules(checked),
            _classification(verdict),
        ],
    )
    return Email(subject=_subject("BLOCKED", "AI_SPAM", checked), text=text, html=markup)
