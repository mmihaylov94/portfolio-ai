"""Asking a model for a short acknowledgement to the visitor.

One call. The model is given one of five briefs, chosen by what the classifier said
the message is about, and returns two or three paragraphs of plain text. The
greeting, the signature and the line saying the reply is automatic are not the
model's to write: ``emails.py`` adds them, so they are always exactly there.

**A reply is a model's output on its way to a stranger's inbox**, so it is checked
like input (:func:`fault`). The form takes any address, which makes the reply a way
to have this system write to somebody who never asked. What that can carry is
bounded here: a short text, plain characters only, with no link, no address and no
phone number in it. A reply that fails is not sent, and the owner's notification
says why. How often a reply may be sent at all is ``replies.py``'s business.
"""

import re
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel

from portfolio_ai.assistant.prompts.loader import CONTACT_BRIEFS, CONTACT_CONTEXT, CONTACT_REPLY
from portfolio_ai.config import get_settings
from portfolio_ai.contact.rules import Checked, Reason
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

# The prompt asks for two or three paragraphs and 80 to 160 words. These are the
# outer limits a reply is refused beyond, set well clear of what was asked for.
MAX_PARAGRAPHS = 5
MAX_CHARACTERS = 2000

# No link and no address: either is what somebody who had talked the model into
# something would want it to write. Four shapes: a scheme, a "www.", an "@", and a
# word that ends the way a web address does, because a mail program makes
# "example.com" a link without being asked. The endings are the common ones and not
# all of them, so this is a bound and not a proof. It also refuses a reply that
# names a product such as "Make.com", which is the safe way to be wrong: no reply
# goes out and the owner is told why. The site's own name is let through.
_LINK_OR_ADDRESS = re.compile(
    r"://|www\.|@|\b(?!mihaylov\.io\b)[a-z0-9-]+\."
    r"(?:com|net|org|io|co|dev|app|info|biz|me|ru|cn|xyz|top|site|online|link|click|shop|live)\b",
    re.IGNORECASE | re.ASCII,
)

# A phone number is the other thing somebody steering the model would want it to
# write: "call us on ...". A run of digits and what numbers are written with (spaces,
# dots, slashes, brackets, and dashes of every kind), and a "+" before it, with nine
# digits or more in it. Nine, because a number that can be rung from elsewhere has its
# area code: "+44 20 7946 0958" holds twelve and "(555) 010-0199" ten, while an ISO
# date holds eight and "80-160 words" five. re.ASCII keeps \d and \s to the ASCII ones.
#
# The dashes run from U+2010 HYPHEN to U+2015 HORIZONTAL BAR: a model writes a number
# with a non-breaking hyphen or an en dash as readily as with "-". They are made with
# chr() and not typed, so that the source holds no character a reviewer cannot see.
_DASHES = "".join(chr(point) for point in range(0x2010, 0x2016))
_NUMBER_RUN = re.compile(rf"\+?[\d(][\d\s()./{_DASHES}-]*\d", re.ASCII)
_PHONE_DIGITS = 9


def _holds_a_phone_number(text: str) -> bool:
    # NFKC first. It writes full-width digits (U+FF10 to U+FF19) as ASCII ones and a
    # no-break space as a space, which the pattern, being ASCII-only, would otherwise
    # not see as either. Dashes it leaves dashes (a non-breaking hyphen becomes a plain
    # U+2010), which is why they are listed above.
    folded = unicodedata.normalize("NFKC", text)
    return any(
        sum(char.isdigit() for char in run) >= _PHONE_DIGITS for run in _NUMBER_RUN.findall(folded)
    )


# The shape the model must answer in. No docstring: it would become the schema's
# description, which the model reads.
class ContactReply(BaseModel):
    paragraphs: list[str]


@dataclass(frozen=True)
class Drafted:
    # The paragraphs, tidied, or None when the model's answer could not be used.
    paragraphs: tuple[str, ...] | None
    # Why not, in a few fixed words, when it could not. Never the reply itself.
    fault: str | None
    usage: CallUsage


def brief(checked: Checked, reason: Reason) -> str:
    """The request for one reply: the brief for this kind of message, filled in.

    ``str.format`` fills the named fields of the brief. Only the brief is read as a
    template; what a visitor typed is handed over as values, so braces in a message
    mean nothing.
    """
    return CONTACT_BRIEFS[reason].text.format(
        email=checked.email,
        subject=checked.subject,
        message=checked.message,
        context=CONTACT_CONTEXT.text,
    )


def fault(paragraphs: tuple[str, ...]) -> str | None:
    """Why these paragraphs cannot be sent, in a few words, or None when they can."""
    if not paragraphs:
        return "empty"
    if len(paragraphs) > MAX_PARAGRAPHS or sum(map(len, paragraphs)) > MAX_CHARACTERS:
        return "too long"
    if not all(paragraph.isprintable() for paragraph in paragraphs):
        return "not plain text"
    if any(_LINK_OR_ADDRESS.search(paragraph) for paragraph in paragraphs):
        return "a link or an address"
    if any(_holds_a_phone_number(paragraph) for paragraph in paragraphs):
        return "a phone number"
    return None


async def draft(checked: Checked, reason: Reason) -> Drafted:
    """Ask the model for the acknowledgement's paragraphs, and check what comes back.

    Raises ``AssistantError`` when OpenAI cannot be reached. No reply is the
    result then, and the caller carries on without one.
    """
    settings = get_settings()
    result = await responses.parse(
        step="contact_reply",
        model=settings.classifier_model,
        effort=settings.classifier_reasoning_effort,
        instructions=CONTACT_REPLY.text,
        input=[{"role": "user", "content": brief(checked, reason)}],
        text_format=ContactReply,
        prompt=CONTACT_REPLY.ref,
    )
    if result.value is None:
        return Drafted(paragraphs=None, fault="unusable", usage=result.usage)

    # Each paragraph flattened to one line, and the empty ones dropped. A walrus
    # (:=) names the flattened text inside the comprehension, so that it is worked
    # out once and both tested and kept.
    paragraphs = tuple(
        flat for paragraph in result.value.paragraphs if (flat := " ".join(paragraph.split()))
    )
    problem = fault(paragraphs)
    if problem is not None:
        return Drafted(paragraphs=None, fault=problem, usage=result.usage)
    return Drafted(paragraphs=paragraphs, fault=None, usage=result.usage)
