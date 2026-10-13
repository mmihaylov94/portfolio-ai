"""Invented conversations, put into the development database through the real assistant.

The analytics were built before the site had visitors to analyse. So that the
commands have something to read, ``datasets/traffic/invented.yaml`` holds
conversations nobody had, and this module plays each one: every question goes
through ``assistant/conversation.chat``, the same function a visitor's message
goes through, which classifies it, searches, answers and stores the turn. The
routes, scores, fallbacks and costs the report then shows are real ones. Only the
questions are made up.

**Development only, and that is a rule with a reason.** A stored turn does not say
where it came from. In production an invented conversation would sit among the
visitors' and be counted with them, and no query could tell them apart afterwards.
Here they can be told apart, by a session id that starts with ``invented-``, and
:func:`clear` removes them by it; but the guard is :func:`_local_only`, which
refuses before the database is read or written.
"""

import datetime as dt
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

import structlog
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from portfolio_ai import strict_yaml
from portfolio_ai.assistant.agent import Done, TurnResult
from portfolio_ai.assistant.conversation import chat
from portfolio_ai.concurrency import gather_limited
from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db import feedback as feedback_db
from portfolio_ai.exceptions import AnalyticsError

log = structlog.get_logger(__name__)

# Where `seed` looks, from the repository root. A folder of its own, because the
# unit tests load every datasets/*.yaml as an eval dataset and this is not one.
DEFAULT_PATH = Path("datasets/traffic/invented.yaml")

# Every invented conversation's session id starts with this. The site's chat mints
# a UUID for each conversation, which never does. The API would accept one that did,
# from a client written to send it, which is one more reason this runs locally only.
SESSION_PREFIX = "invented-"

# Conversations played at once. Within one, the questions go in order: each is
# answered with the earlier ones as its history.
CONCURRENCY = 4

# Roughly what one question costs to classify and answer, for the plan a dry run
# prints. The real figure is in the summary afterwards.
ESTIMATED_COST_PER_QUESTION = Decimal("0.001")

Kind = Literal["answerable", "gap", "small_talk", "off_topic"]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=60)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class InventedMessage(BaseModel):
    """One question, and what it was written to be."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: Text
    kind: Kind
    # Which content gap a `gap` question belongs to. The labels are compared with
    # what the clustering makes of the questions.
    topic: Slug | None = None
    # The message before this one, asked again in other words.
    rephrases_previous: bool = False
    rating: Literal[-1, 1] | None = None
    comment: Text | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.kind == "gap") != (self.topic is not None):
            raise ValueError("a gap question names its topic, and only a gap question does")
        if self.comment is not None and self.rating is None:
            raise ValueError("a comment comes with a rating")
        return self


class InventedConversation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Slug
    # How long ago it is made to have happened. Under the 90 days chat is kept for.
    days_ago: int = Field(ge=0, le=89)
    messages: tuple[InventedMessage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _a_rephrase_has_something_before_it(self) -> Self:
        if self.messages[0].rephrases_previous:
            raise ValueError("the first message of a conversation rephrases nothing")
        return self

    @property
    def session_id(self) -> str:
        return f"{SESSION_PREFIX}{self.id}"


class InventedTraffic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: Text
    conversations: tuple[InventedConversation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [conversation.id for conversation in self.conversations]
        repeated = sorted({item for item in ids if ids.count(item) > 1})
        if repeated:
            raise ValueError(f"conversation ids must be unique; repeated: {', '.join(repeated)}")
        return self

    @property
    def messages(self) -> list[InventedMessage]:
        return [message for conversation in self.conversations for message in conversation.messages]


def load(path: Path) -> InventedTraffic:
    """Read and validate the file. Any problem is an ``AnalyticsError`` naming it."""
    try:
        # Strictly, as the eval datasets are read: a field written twice is an error
        # and not the second one quietly winning.
        raw = strict_yaml.load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnalyticsError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise AnalyticsError(f"{path} is not valid YAML: {exc}") from exc

    try:
        return InventedTraffic.model_validate(raw)
    except ValidationError as exc:
        raise AnalyticsError(f"{path} does not hold together.\n{exc}") from exc


def _local_only() -> None:
    environment = get_settings().environment
    if environment != "local":
        raise AnalyticsError(
            f"ENVIRONMENT is {environment!r}. Invented conversations go into the development "
            "database only: anywhere else they would be counted as visitors."
        )


@dataclass(frozen=True)
class Played:
    """One invented question and what the assistant made of it."""

    conversation: str
    message: InventedMessage
    result: TurnResult


@dataclass(frozen=True)
class SeedReport:
    conversations: int
    played: list[Played]

    @property
    def cost(self) -> Decimal:
        return sum((item.result.cost for item in self.played), start=Decimal(0))

    @property
    def routes(self) -> Counter[tuple[str, str]]:
        """How many questions of each kind went down each route."""
        return Counter((item.message.kind, item.result.classification) for item in self.played)

    @property
    def fallbacks(self) -> Counter[str]:
        """How many questions of each kind were answered with a fallback."""
        return Counter(item.message.kind for item in self.played if item.result.fallback_used)


async def stored() -> int:
    """How many invented conversations the development database holds now.

    Refuses anywhere else, like everything here that reads or writes: the dry run
    calls this, and a dry run that passed in production would be a promise that the
    real one works there.
    """
    _local_only()
    return await analytics_db.count_conversations(SESSION_PREFIX)


async def _play(
    conversation: InventedConversation, on_played: Callable[[Played], None] | None
) -> list[Played]:
    played: list[Played] = []

    for message in conversation.messages:
        result: TurnResult | None = None
        # chat() is an async generator: it yields events as the answer is produced,
        # and the turn is stored only when the last of them, Done, is reached. So
        # the loop has to run to the end even though one event is all that is kept.
        async for event in chat(conversation.session_id, message.text):
            if isinstance(event, Done):
                result = event.result
        if result is None or result.message_id is None:  # pragma: no cover - see above
            raise RuntimeError(f"{conversation.id}: a turn ended without being stored")

        if message.rating is not None:
            await feedback_db.record_feedback(
                session_id=conversation.session_id,
                message_id=result.message_id,
                rating=message.rating,
                comment=message.comment,
            )

        item = Played(conversation=conversation.id, message=message, result=result)
        played.append(item)
        if on_played is not None:
            on_played(item)

    # Last, and once: the turns were stored as of now, seconds apart, and the whole
    # conversation moves back together.
    if conversation.days_ago:
        await analytics_db.backdate_conversation(
            conversation.session_id, dt.timedelta(days=conversation.days_ago)
        )
    return played


async def seed(
    traffic: InventedTraffic, *, on_played: Callable[[Played], None] | None = None
) -> SeedReport:
    """Play every conversation and store it. Costs about a tenth of a cent a question.

    Refused outside development, and refused when invented conversations are already
    stored: playing them twice would double every count, and a conversation's
    second playing would be answered with its first as history.

    If an OpenAI call fails part-way, what was stored so far stays. ``seed --clear``
    removes it, and the whole thing is run again.
    """
    already = await stored()
    if already:
        raise AnalyticsError(
            f"{already} invented conversation(s) are already stored. "
            "Run `seed --clear` first, then seed again."
        )

    results = await gather_limited(
        CONCURRENCY, *(_play(conversation, on_played) for conversation in traffic.conversations)
    )
    report = SeedReport(
        conversations=len(traffic.conversations),
        # gather_limited returns one list per conversation; this flattens them. Read
        # it as two nested for-loops, written in the order they would be nested.
        played=[item for conversation in results for item in conversation],
    )

    log.info(
        "invented_traffic_seeded",
        conversations=report.conversations,
        questions=len(report.played),
        cost_usd=str(report.cost),
    )
    return report


@dataclass(frozen=True)
class Cleared:
    conversations: int
    gaps: int


async def clear() -> Cleared:
    """Delete every invented conversation, and the content gaps made from them.

    All the gaps, and not only some: in development every gap came from an invented
    question, and one left behind would be recognised by the next seeding's refresh
    as an old acquaintance, with a label and a status nobody chose this time.
    """
    _local_only()
    removed = Cleared(
        conversations=await analytics_db.delete_conversations(SESSION_PREFIX),
        gaps=await analytics_db.delete_gaps(),
    )
    log.info("invented_traffic_cleared", conversations=removed.conversations, gaps=removed.gaps)
    return removed
