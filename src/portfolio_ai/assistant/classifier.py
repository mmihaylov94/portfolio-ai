"""Deciding what kind of message this is, before anything expensive happens.

Every message is sorted into one of three routes, and the route decides the cost:

- ``out_of_scope`` -- a fixed reply. This call is the only one the message costs.
- ``small_talk`` -- one short reply, no search.
- ``mihail_related`` -- the full knowledge-base answer: search, then answer.

The prompt is n8n's, word for word. What changed is what it is shown: n8n gave the
classifier the new message alone, so "yes please" -- the natural reply to Rachel
ending an answer with "want more detail?" -- was classified as small talk, and went
to the one route that cannot search. Measured before this was written: with the
previous exchange included, those follow-ups route correctly and nothing else moved.

Structured output rather than asking for a bare word back. The API holds the model
to the schema below, so the answer is always exactly one of the three labels, and
there is no "Mihail-related." with a full stop to parse around.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import structlog
from pydantic import BaseModel

from portfolio_ai.assistant.memory import HistoryMessage, as_input
from portfolio_ai.assistant.prompts.loader import CLASSIFIER
from portfolio_ai.config import ReasoningEffort
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

log = structlog.get_logger(__name__)

type Classification = Literal["out_of_scope", "small_talk", "mihail_related"]


# The shape the classifier must answer in. Its JSON schema is sent to the API, and the
# model reads it: the class name becomes the schema's name and a docstring would become
# its description. So no docstring -- this comment would otherwise be text the model
# sees on every message, which the n8n prompt never contained.
class MessageCategory(BaseModel):
    category: Classification


@dataclass(frozen=True)
class ClassifierResult:
    label: Classification
    usage: CallUsage


async def classify(
    message: str,
    previous: Sequence[HistoryMessage],
    *,
    model: str,
    effort: ReasoningEffort | None = None,
) -> ClassifierResult:
    """Route one message, given the exchange before it (empty on a first message)."""
    result = await responses.parse(
        step="classify",
        model=model,
        instructions=CLASSIFIER.text,
        input=[*as_input(previous), {"role": "user", "content": message}],
        text_format=MessageCategory,
        effort=effort,
        prompt=CLASSIFIER.ref,
    )

    if result.value is None:
        # A refusal or an unreadable answer. The prompt's own tie-breaker decides:
        # "when in doubt between mihail_related and out_of_scope, choose
        # mihail_related". Wrongly searching costs a fraction of a cent; wrongly
        # refusing loses a visitor's real question.
        log.warning("classifier_fallback", label="mihail_related")
        return ClassifierResult(label="mihail_related", usage=result.usage)

    return ClassifierResult(label=result.value.category, usage=result.usage)
