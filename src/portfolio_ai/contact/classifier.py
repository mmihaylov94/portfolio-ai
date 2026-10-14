"""Asking a model what a contact form message is: spam or not, about what, safe to answer.

The second look. ``rules.py`` has already cleaned the message and scored it, and
anything it called spam never gets here. What is left is what fixed rules cannot
judge: whether a message is a real inquiry, what it is really about (the reason a
visitor picks in the form is a hint), and whether it is safe to answer automatically.

One call, with a structured answer, the pattern of ``assistant/classifier.py``. It
uses that classifier's model and effort: the cheap, fast one, doing the same kind of
job.

**The verdict is a model's output, and is treated as input.** It was written from
what a stranger typed into a form. Its two confidences are kept between 0 and 1
whatever the model returned, and one that is not a number at all means no verdict.
Its explanation is flattened to one clipped line of characters that can be printed
before it goes into the owner's notification, or onto a terminal.
"""

import json
import math
from dataclasses import dataclass

from pydantic import BaseModel

from portfolio_ai.assistant.prompts.loader import CONTACT_CLASSIFIER
from portfolio_ai.config import get_settings
from portfolio_ai.contact.rules import Checked, Reason
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

# How much of the model's explanation the owner is shown.
EXPLANATION_LENGTH = 300


# The shape the model must answer in. No docstring, for the reason the assistant's
# classifier has none: it would become the schema's description, which the model reads.
class ContactVerdict(BaseModel):
    is_spam: bool
    spam_confidence: float
    detected_reason: Reason
    reason_confidence: float
    should_auto_reply: bool
    explanation: str


@dataclass(frozen=True)
class Verdict:
    """What the model said, tidied."""

    is_spam: bool
    spam_confidence: float
    detected_reason: Reason
    reason_confidence: float
    should_auto_reply: bool
    explanation: str


@dataclass(frozen=True)
class Classified:
    # None when the model's answer could not be used: a refusal, or not the shape.
    verdict: Verdict | None
    usage: CallUsage


def brief(checked: Checked) -> str:
    """What the model is shown: the cleaned submission, and what the rules made of it.

    The workflow's own wording. Two details keep it the same text n8n sent:
    ``suspicious`` is written as JavaScript writes a boolean, in lower case, and the
    rule hits are a JSON list with nothing after its commas, which is how
    JavaScript's ``JSON.stringify`` writes one and not how ``json.dumps`` does
    unless told.
    """
    hits = json.dumps(list(checked.rule_hits), separators=(",", ":"))
    return (
        "Classify this contact form submission.\n"
        "\n"
        "Submission:\n"
        f"- email: {checked.email}\n"
        f"- subject: {checked.subject}\n"
        f"- message: {checked.message}\n"
        f"- submitted_reason: {checked.reason}\n"
        "\n"
        "Rule-based precheck results:\n"
        f"- spam_score: {checked.spam_score}\n"
        f"- suspicious: {str(checked.suspicious).lower()}\n"
        f"- rule_hits: {hits}\n"
        "\n"
        "Return the JSON classification object."
    )


def _share(value: float) -> float:
    """A confidence, kept between 0 and 1. min and max, nested, are the usual clamp."""
    return min(1.0, max(0.0, value))


def _one_line(text: str) -> str:
    """The model's explanation as one clipped line of characters that can be printed.

    It goes into an email and, from ``contact try``, onto a terminal, where an
    escape sequence clears the screen or recolours it. ``str.isprintable`` is False
    for every control and format character, so what is left is text.
    """
    flat = " ".join(text.split())
    return "".join(char for char in flat if char.isprintable())[:EXPLANATION_LENGTH]


async def classify(checked: Checked) -> Classified:
    """Ask the model about one submission the rules let through.

    Raises ``AssistantError`` when OpenAI cannot be reached, as every call through
    ``responses.parse`` does. The caller decides what a message is worth without a
    verdict; it is never lost for want of one.
    """
    settings = get_settings()
    result = await responses.parse(
        step="contact_classify",
        model=settings.classifier_model,
        effort=settings.classifier_reasoning_effort,
        instructions=CONTACT_CLASSIFIER.text,
        input=[{"role": "user", "content": brief(checked)}],
        text_format=ContactVerdict,
        prompt=CONTACT_CLASSIFIER.ref,
    )
    said = result.value
    # A confidence that is not a number at all (NaN, or an infinity) is no verdict.
    # Clamped, NaN would come out as 0: "certainly not spam", from a model that said
    # nothing of the kind. The API's own schema should never let one through.
    if said is None or not (
        math.isfinite(said.spam_confidence) and math.isfinite(said.reason_confidence)
    ):
        return Classified(verdict=None, usage=result.usage)

    return Classified(
        verdict=Verdict(
            is_spam=said.is_spam,
            spam_confidence=_share(said.spam_confidence),
            detected_reason=said.detected_reason,
            reason_confidence=_share(said.reason_confidence),
            should_auto_reply=said.should_auto_reply,
            explanation=_one_line(said.explanation),
        ),
        usage=result.usage,
    )
