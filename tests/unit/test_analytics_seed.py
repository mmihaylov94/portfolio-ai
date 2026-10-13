"""The invented conversations: the file itself, the rules it is held to, and the
refusals that keep it out of anywhere but the development database.

Playing a conversation through the assistant and storing it is tested against a
real Postgres, in tests/integration/test_analytics_seed.py.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from portfolio_ai.analytics import seed
from portfolio_ai.analytics.seed import InventedConversation, InventedMessage, InventedTraffic
from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.exceptions import AnalyticsError

INVENTED = Path(__file__).resolve().parents[2] / seed.DEFAULT_PATH


def _conversation(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "one",
        "days_ago": 3,
        "messages": [{"text": "Hello", "kind": "small_talk"}],
        **overrides,
    }


def _traffic(*conversations: dict[str, Any]) -> dict[str, Any]:
    return {"description": "A test.", "conversations": list(conversations or [_conversation()])}


# --- the committed file -----------------------------------------------------------


def test_the_committed_file_loads_and_is_the_size_its_header_says() -> None:
    traffic = seed.load(INVENTED)

    assert len(traffic.conversations) == 28
    assert sum(message.rephrases_previous for message in traffic.messages) == 8
    assert len({message.topic for message in traffic.messages if message.topic}) == 6


def test_the_committed_file_names_nobody_and_links_nowhere() -> None:
    """It is public, and it is written to look like what visitors type. So it holds
    no address of any kind: nothing in it should be mistakable for a real person's
    message, and a made-up address has a way of belonging to somebody."""
    for message in seed.load(INVENTED).messages:
        assert "@" not in message.text, message.text
        assert not re.search(r"https?://|www\.", message.text), message.text


def test_every_conversation_gets_a_session_id_no_browser_would_mint() -> None:
    """The site's chat mints a UUID for every conversation. `seed --clear` deletes
    by this prefix, so it has to be one the site never produces."""
    for conversation in seed.load(INVENTED).conversations:
        assert conversation.session_id == f"invented-{conversation.id}"
        assert not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", conversation.session_id)


# --- the rules --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "problem"),
    [
        ({"text": "Rates?", "kind": "gap"}, "names its topic"),
        ({"text": "Hello", "kind": "small_talk", "topic": "rates"}, "names its topic"),
        ({"text": "Hello", "kind": "small_talk", "comment": "Nice."}, "comes with a rating"),
        ({"text": "Hello", "kind": "small_talk", "rating": 0}, "rating"),
        ({"text": "Hello", "kind": "chit_chat"}, "kind"),
        ({"text": "   ", "kind": "small_talk"}, "text"),
        ({"text": "Hello", "kind": "small_talk", "kinds": "x"}, "kinds"),
        ({"text": "Rates?", "kind": "gap", "topic": "Day Rates"}, "topic"),
    ],
    ids=[
        "gap-without-topic",
        "topic-without-gap",
        "comment-without-rating",
        "rating-out-of-range",
        "unknown-kind",
        "blank-text",
        "misspelt-field",
        "topic-not-a-slug",
    ],
)
def test_a_message_that_does_not_hold_together_is_refused(
    message: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ValidationError, match=problem):
        InventedMessage.model_validate(message)


def test_the_first_message_cannot_rephrase_anything() -> None:
    first = {"text": "Rates?", "kind": "gap", "topic": "rates", "rephrases_previous": True}

    with pytest.raises(ValidationError, match="rephrases nothing"):
        InventedConversation.model_validate(_conversation(messages=[first]))


@pytest.mark.parametrize("days_ago", [-1, 90])
def test_a_conversation_is_set_within_the_days_chat_is_kept_for(days_ago: int) -> None:
    """Ninety days back, the nightly purge would be entitled to delete it."""
    with pytest.raises(ValidationError, match="days_ago"):
        InventedConversation.model_validate(_conversation(days_ago=days_ago))


def test_two_conversations_cannot_share_an_id() -> None:
    """They would share a session, and the second would be answered with the first
    as its history."""
    with pytest.raises(ValidationError, match="repeated: one"):
        InventedTraffic.model_validate(_traffic(_conversation(), _conversation()))


def test_a_conversation_needs_a_message_and_a_file_needs_a_conversation() -> None:
    with pytest.raises(ValidationError):
        InventedConversation.model_validate(_conversation(messages=[]))
    with pytest.raises(ValidationError):
        InventedTraffic.model_validate({"description": "Empty.", "conversations": []})


# --- reading the file -------------------------------------------------------------


def test_a_missing_file_is_named(tmp_path: Path) -> None:
    with pytest.raises(AnalyticsError, match="Cannot read"):
        seed.load(tmp_path / "nowhere.yaml")


def test_a_file_that_is_not_yaml_is_said_to_be(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("conversations: [unclosed", encoding="utf-8")

    with pytest.raises(AnalyticsError, match="not valid YAML"):
        seed.load(path)


def test_a_field_written_twice_is_refused(tmp_path: Path) -> None:
    """Plain YAML would keep the second `kind` without a word. Pydantic could not
    object either: by the time it is handed the message there is only one."""
    path = tmp_path / "twice.yaml"
    path.write_text(
        "description: Twice.\n"
        "conversations:\n"
        "  - id: one\n"
        "    days_ago: 1\n"
        "    messages:\n"
        "      - text: Hello\n"
        "        kind: gap\n"
        "        kind: small_talk\n",
        encoding="utf-8",
    )

    with pytest.raises(AnalyticsError, match="'kind' appears twice"):
        seed.load(path)


def test_a_file_that_breaks_a_rule_reports_where(tmp_path: Path) -> None:
    path = tmp_path / "wrong.yaml"
    path.write_text(
        "description: Wrong.\n"
        "conversations:\n"
        "  - id: one\n"
        "    days_ago: 1\n"
        "    messages:\n"
        "      - text: Rates?\n"
        "        kind: gap\n",
        encoding="utf-8",
    )

    with pytest.raises(AnalyticsError, match=r"conversations\.0\.messages\.0") as refusal:
        seed.load(path)
    assert "names its topic" in str(refusal.value)


# --- development only -------------------------------------------------------------


@pytest.fixture
def untouchable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything reaches for the database at all."""

    async def never(*args: object, **kwargs: object) -> int:  # ruff: ignore[unused-async]
        raise AssertionError(f"the database was touched: {args} {kwargs}")

    for name in ("count_conversations", "delete_conversations", "backdate_conversation"):
        monkeypatch.setattr(analytics_db, name, never)


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    get_settings.cache_clear()


@pytest.mark.usefixtures("production", "untouchable_database")
async def test_nothing_here_runs_in_production() -> None:
    """Seeding, clearing, and the count the dry run prints. An invented
    conversation stored in production is a visitor as far as anything can tell
    afterwards, and a dry run that passed there would be a promise that the real
    one works."""
    traffic = InventedTraffic.model_validate(_traffic())

    for refused in (seed.seed(traffic), seed.clear(), seed.stored()):
        with pytest.raises(AnalyticsError, match="development database only"):
            await refused


async def test_seeding_twice_is_refused_before_anything_is_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def three(prefix: str) -> int:  # ruff: ignore[unused-async]
        assert prefix == "invented-"
        return 3

    monkeypatch.setattr(analytics_db, "count_conversations", three)

    with pytest.raises(AnalyticsError, match="3 invented conversation"):
        # No fake OpenAI is installed: a question asked would fail on the network.
        await seed.seed(InventedTraffic.model_validate(_traffic()))
