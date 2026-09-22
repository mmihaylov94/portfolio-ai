"""What the model is shown of the conversation so far.

Memory here is deliberately simple, and the same shape n8n's was: the visitor's
questions and Rachel's final answers, as plain text, for the last few exchanges.
Not the searches she ran or what they returned. A follow-up question gets a fresh
search, which is cheaper than replaying twenty chunks from three turns ago and
retrieves better, because the query is written for the question actually asked.

Loading and saving live in ``db/chat.py``. This module is only the shaping, which
is why it can be tested without a database.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from openai.types.responses import EasyInputMessageParam, ResponseInputItemParam


@dataclass(frozen=True)
class HistoryMessage:
    """One message from earlier in the conversation."""

    role: Literal["user", "assistant"]
    content: str


def window(history: Sequence[HistoryMessage], turns: int) -> list[HistoryMessage]:
    """The last ``turns`` exchanges -- ``2 * turns`` messages.

    Counted in exchanges because that is what n8n's setting meant (see
    ``memory_window_turns`` in config.py), and because cutting a conversation half
    way through an exchange would show the model an answer without its question.
    """
    if turns <= 0:
        return []
    return list(history[-2 * turns :])


def previous_exchange(history: Sequence[HistoryMessage]) -> list[HistoryMessage]:
    """The last two messages -- normally the last question and its answer.

    This is what the classifier sees alongside a new message. It is enough to know
    that "yes please" is accepting an offer of more detail about Laravel, and more
    than that only makes the classifier slower and more expensive; it is not trying
    to follow the conversation, only to route one message.
    """
    return list(history[-2:])


def as_input(history: Sequence[HistoryMessage]) -> list[ResponseInputItemParam]:
    """History in the shape the Responses API takes as ``input``."""
    return [
        EasyInputMessageParam(role=message.role, content=message.content) for message in history
    ]
