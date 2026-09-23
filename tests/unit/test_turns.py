"""The turn registry: answers that finish whoever is listening, and end however they end.

These drive ``Turns`` directly, with small async generators standing in for the
assistant. The HTTP tests in test_api.py cover the same guarantees through the
endpoints; these pin down the mechanism underneath them.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from portfolio_ai.api import turns as turns_module
from portfolio_ai.api.turns import Turns
from portfolio_ai.assistant.agent import Classified, Done, Event, Token, TurnResult
from portfolio_ai.exceptions import AssistantError

SESSION = "session-under-test"


def _result(reply: str) -> TurnResult:
    return TurnResult(
        reply=reply,
        classification="small_talk",
        citations=[],
        searches=[],
        retrieved_chunk_ids=[],
        top_score=None,
        fallback_used=False,
        links_removed=0,
        calls=[],
        model="gpt-5-mini",
        latency_ms=1,
        first_token_ms=1,
    )


async def _slow_answer(finished: list[str], *, pause: float = 0.01) -> AsyncIterator[Event]:
    """An answer that takes a moment between words, and notes when it finishes."""
    yield Classified("small_talk")
    for word in ("Hello", " there"):
        await asyncio.sleep(pause)
        yield Token(word)
    finished.append("Hello there")
    yield Done(_result("Hello there"))


async def test_an_abandoned_turn_still_runs_to_the_end() -> None:
    """The whole point: the reader goes away after one event, the answer does not."""
    turns = Turns()
    finished: list[str] = []
    turn = turns.start(SESSION, _slow_answer(finished))

    events = turn.events()
    first = await anext(events)
    assert first == Classified("small_talk")
    await events.aclose()  # the visitor leaves

    await turns.drain(grace_seconds=5)

    assert finished == ["Hello there"]
    assert not turns.busy(SESSION)


async def test_a_reader_gets_every_event_and_stops_after_done() -> None:
    turns = Turns()
    turn = turns.start(SESSION, _slow_answer([]))

    received = [event async for event in turn.events()]

    assert [type(event).__name__ for event in received] == [
        "Classified",
        "Token",
        "Token",
        "Done",
    ]


async def test_a_failed_turn_raises_in_the_reader() -> None:
    async def failing() -> AsyncIterator[Event]:
        yield Classified("mihail_related")
        await asyncio.sleep(0)
        raise AssistantError("OpenAI call failed (APIConnectionError): scripted")

    turns = Turns()
    turn = turns.start(SESSION, failing())

    with pytest.raises(AssistantError, match="scripted"):
        async for _ in turn.events():
            pass


class _BrokenLog:
    """A logger whose every call raises, as a broken handler would."""

    def __getattr__(self, name: str) -> Callable[..., None]:
        def fail(*args: object, **kwargs: object) -> None:  # ruff: ignore[unused-function-argument]
            raise RuntimeError("the log handler is broken")

        return fail


@pytest.mark.parametrize(
    "error",
    [AssistantError("OpenAI call failed (APIConnectionError): scripted"), KeyError("a bug")],
    ids=["expected-failure", "bug"],
)
async def test_the_reader_hears_of_a_failure_even_when_logging_it_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """The reader is told before anything is logged. A log call can raise too, and if
    it came first, the reader would wait on a queue nobody writes to again."""
    monkeypatch.setattr(turns_module, "log", _BrokenLog())

    async def failing() -> AsyncIterator[Event]:
        yield Classified("mihail_related")
        await asyncio.sleep(0)
        raise error

    turns = Turns()
    turn = turns.start(SESSION, failing())
    task = turns._running[SESSION]

    async with asyncio.timeout(1):
        with pytest.raises(type(error)):
            async for _ in turn.events():
                pass

    # The logging failure is not swallowed: the task ends with it. Awaiting the task
    # here also keeps asyncio from reporting an exception nobody retrieved.
    with pytest.raises(RuntimeError, match="broken"):
        await task


async def test_a_turn_past_its_deadline_fails_and_frees_its_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turns_module, "TURN_DEADLINE_SECONDS", 0.05)

    async def stuck() -> AsyncIterator[Event]:
        yield Classified("mihail_related")
        await asyncio.sleep(60)
        yield Token("never")

    turns = Turns()
    turn = turns.start(SESSION, stuck())

    with pytest.raises(AssistantError, match="took longer than"):
        async for _ in turn.events():
            pass

    await asyncio.sleep(0)  # let the done-callback run
    assert not turns.busy(SESSION)
    assert turns.in_flight == 0


async def test_shutdown_waits_then_cancels_and_the_reader_is_told() -> None:
    async def stuck() -> AsyncIterator[Event]:
        yield Classified("mihail_related")
        await asyncio.sleep(60)
        yield Token("never")

    turns = Turns()
    turn = turns.start(SESSION, stuck())
    events = turn.events()
    await anext(events)

    await turns.drain(grace_seconds=0.05)

    with pytest.raises(AssistantError, match="shutting down"):
        await anext(events)


async def test_one_turn_per_conversation() -> None:
    turns = Turns()
    release = asyncio.Event()

    async def held() -> AsyncIterator[Event]:
        await release.wait()
        yield Done(_result("done"))

    turns.start(SESSION, held())
    assert turns.busy(SESSION)
    assert not turns.busy("another-session")

    with pytest.raises(RuntimeError, match="already running"):
        turns.start(SESSION, held())

    release.set()
    await turns.drain(grace_seconds=5)


async def test_a_finished_turn_does_not_block_the_next_one() -> None:
    """busy() looks at the task, not just the dictionary: a done-callback runs on the
    loop's next pass, so for a moment a finished turn is still in the dictionary."""
    turns = Turns()
    first = turns.start(SESSION, _slow_answer([], pause=0))
    async for _ in first.events():
        pass

    turns.start(SESSION, _slow_answer([], pause=0))  # does not raise

    await turns.drain(grace_seconds=5)


async def test_a_late_callback_does_not_remove_a_newer_turn() -> None:
    """The callback for a finished turn must not evict its successor, which would
    lift the one-at-a-time limit in the middle of an answer."""
    turns = Turns()
    release = asyncio.Event()

    async def held() -> AsyncIterator[Event]:
        await release.wait()
        yield Done(_result("done"))

    turns.start(SESSION, held())
    stale = asyncio.create_task(asyncio.sleep(0))
    await stale

    turns._forget(SESSION, stale)

    assert turns.busy(SESSION)
    release.set()
    await turns.drain(grace_seconds=5)
