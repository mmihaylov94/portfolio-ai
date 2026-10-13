"""Seeding invented conversations, end to end.

Real database, real migrations, the real path a visitor's message takes
(``conversation.chat``). Only the model is a fake. What is checked is what the
seeding adds around that path: the session names, the thumbs, the move into the
past, the refusal to run twice, and that clearing takes the invented conversations
and leaves a visitor's alone.
"""

import datetime as dt

import corpus
import pytest
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.analytics import seed
from portfolio_ai.analytics.seed import InventedTraffic, Played
from portfolio_ai.assistant.conversation import chat
from portfolio_ai.db.pool import get_pool
from portfolio_ai.exceptions import AnalyticsError

pytestmark = pytest.mark.integration

VISITOR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

TRAFFIC = InventedTraffic.model_validate(
    {
        "description": "Two conversations, for the test.",
        "conversations": [
            {
                "id": "one",
                "days_ago": 5,
                "messages": [
                    {"text": "Hello", "kind": "small_talk"},
                    {
                        "text": "Does Mihail work with Laravel?",
                        "kind": "answerable",
                        "rating": -1,
                        "comment": "Too short.",
                    },
                ],
            },
            {
                "id": "two",
                "days_ago": 0,
                "messages": [{"text": "What is his day rate?", "kind": "gap", "topic": "rates"}],
            },
        ],
    }
)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    # One conversation at a time. The fake hands out its scripted replies in the
    # order the calls arrive, and two conversations played at once would take each
    # other's.
    monkeypatch.setattr(seed, "CONCURRENCY", 1)
    return install_fake_openai(monkeypatch)


@pytest.fixture(autouse=True)
async def knowledge_base() -> None:
    await corpus.clear()
    await corpus.seed(
        "tech-stack",
        "Tech Stack",
        [("Does Mihail work with Laravel?", "Yes, it is his main PHP framework.", corpus.axis(0))],
    )


def _script(fake: FakeOpenAI) -> None:
    """The replies TRAFFIC needs, in the order its questions are asked."""
    fake.classify("small_talk")
    fake.say("Hello! How can I help?")
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")
    fake.classify("mihail_related")
    fake.search("day rate")
    fake.say("I don't have that information in my knowledge base.")


async def _all(sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params)
        return [dict(row) for row in await cur.fetchall()]


async def test_conversations_are_played_stored_rated_and_moved_into_the_past(
    fake: FakeOpenAI,
) -> None:
    _script(fake)
    seen: list[Played] = []

    report = await seed.seed(TRAFFIC, on_played=seen.append)

    # --- what came back
    assert fake.pending == 0, "every scripted reply was asked for"
    assert seen == report.played, "each question is reported as it is answered"
    assert report.conversations == 2
    assert [(item.conversation, item.message.text) for item in report.played] == [
        ("one", "Hello"),
        ("one", "Does Mihail work with Laravel?"),
        ("two", "What is his day rate?"),
    ]
    assert report.routes == {
        ("small_talk", "small_talk"): 1,
        ("answerable", "mihail_related"): 1,
        ("gap", "mihail_related"): 1,
    }
    assert report.fallbacks == {"gap": 1}
    assert report.cost > 0

    # --- what is stored
    rows = await _all(
        """
        select s.session_id, m.role, m.content, m.created_at, m.classification, m.fallback_used
        from chat_messages m
        join chat_sessions s on s.id = m.session_id
        order by m.id
        """
    )
    assert [(row["session_id"], row["role"], row["content"]) for row in rows] == [
        ("invented-one", "user", "Hello"),
        ("invented-one", "assistant", "Hello! How can I help?"),
        ("invented-one", "user", "Does Mihail work with Laravel?"),
        ("invented-one", "assistant", "Yes, Laravel is his main PHP framework."),
        ("invented-two", "user", "What is his day rate?"),
        ("invented-two", "assistant", "I don't have that information in my knowledge base."),
    ]
    assert rows[5]["fallback_used"] is True

    # --- the second question was answered with the first as its history
    assert fake.requests[2]["input"][:2] == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hello! How can I help?"},
    ]

    # --- when
    now = dt.datetime.now(dt.UTC)
    for row in rows:
        age = now - row["created_at"]  # type: ignore[operator]
        expected = dt.timedelta(days=5 if row["session_id"] == "invented-one" else 0)
        assert abs(age - expected) < dt.timedelta(minutes=1), row
    sessions = await _all("select session_id, last_seen_at from chat_sessions order by session_id")
    assert now - sessions[0]["last_seen_at"] > dt.timedelta(days=4)  # type: ignore[operator]

    # --- the thumbs down, on the answer it was left on
    [vote] = await _all(
        "select f.rating, f.comment, m.content from message_feedback f "
        "join chat_messages m on m.id = f.message_id"
    )
    assert vote == {
        "rating": -1,
        "comment": "Too short.",
        "content": "Yes, Laravel is his main PHP framework.",
    }


async def test_seeding_twice_is_refused_and_asks_nothing(fake: FakeOpenAI) -> None:
    _script(fake)
    await seed.seed(TRAFFIC)
    calls = len(fake.requests)

    with pytest.raises(AnalyticsError, match="2 invented conversation"):
        await seed.seed(TRAFFIC)

    assert len(fake.requests) == calls
    assert await seed.stored() == 2


async def test_clearing_takes_the_invented_conversations_and_leaves_a_visitors(
    fake: FakeOpenAI,
) -> None:
    fake.classify("small_talk")
    fake.say("Hi there.")
    async for _ in chat(VISITOR, "Hi"):
        pass
    _script(fake)
    await seed.seed(TRAFFIC)

    removed = await seed.clear()

    assert removed == 2
    assert await seed.stored() == 0
    rows = await _all(
        "select s.session_id, m.content from chat_messages m "
        "join chat_sessions s on s.id = m.session_id order by m.id"
    )
    assert rows == [
        {"session_id": VISITOR, "content": "Hi"},
        {"session_id": VISITOR, "content": "Hi there."},
    ]
    assert await _all("select id from message_feedback") == []


async def test_clearing_nothing_removes_nothing() -> None:
    assert await seed.clear() == 0
