"""Answers being written right now -- and why they do not stop when a visitor leaves.

The one idea in this module: **a turn outlives the request that asked for it.**

The obvious way to stream an answer is to run the assistant inside the request:
the endpoint iterates ``conversation.chat()`` and forwards each event. It works
until a visitor closes the tab. Then the server cancels the request, the
cancellation lands inside the assistant, and the turn dies half-written -- after the
classifier and the search have been paid for, and before anything was stored. The
question disappears from the analytics, and what it cost is never counted against
the daily limit, which is the one thing meant to bound the bill whatever a caller
does. A script that hangs up after a second would spend without being counted.

So the turn runs in a task of its own, and the request only watches it:

    request --starts--> task: conversation.chat() --each event--> queue
    request <--reads-- queue

Cancelling the request cancels the reading and nothing else. The task carries on,
writes the answer nobody is waiting for, stores it, and counts it. That is the
trade: a few tenths of a cent for the unread rest of an answer, in exchange for
every started turn being recorded.

Three asyncio details this depends on, each easy to get wrong:

- **A task needs a reference.** The event loop keeps only a weak reference to a
  task, so one created and forgotten can be garbage-collected mid-flight. The
  registry below holds every running turn until it finishes.
- **Never await the task from the request.** Awaiting a task and then being
  cancelled cancels the task too; that is how ``await`` passes cancellation down.
  The request reads from the queue instead, and cancelling a ``queue.get()`` touches
  nothing else.
- **A task copies the context it was created in.** Anything bound with structlog's
  ``bind_contextvars`` -- the request id -- is still on every line the turn logs,
  even after the request that started it has finished.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from functools import partial

import structlog

from portfolio_ai.assistant.agent import Done, Event
from portfolio_ai.exceptions import AssistantError, PortfolioAIError

log = structlog.get_logger(__name__)

# The longest one answer may take before it is abandoned. A normal one takes under
# half a minute at the default reasoning effort. This is for the stuck one: the SDK
# allows 30 s a call and three retries, and an answer is up to five calls, so
# without a deadline one bad minute at OpenAI could hold a conversation's slot (and
# one of the global ones) for several.
TURN_DEADLINE_SECONDS = 120


@dataclass(frozen=True)
class Failed:
    """The turn ended without an answer.

    ``error`` is for the logs and for ``errors.py`` to turn into a status code. It
    never reaches a visitor as it is.
    """

    error: Exception


# What travels through the queue: the assistant's own events, then exactly one of
# Done or Failed.
type Item = Event | Failed


class Turn:
    """One answer being written, as seen by the request that asked for it."""

    def __init__(self, session_id: str, queue: asyncio.Queue[Item]) -> None:
        self.session_id = session_id
        self._queue = queue

    # AsyncGenerator rather than the looser AsyncIterator, because a caller that
    # stops reading early may want to close it (aclose), and only a generator has
    # that.
    async def events(self) -> AsyncGenerator[Event, None]:
        """The turn's events as they happen. Ends after ``Done``; raises on failure.

        Raising rather than yielding a ``Failed`` keeps the two readers simple: the
        JSON endpoint lets the error reach the exception handlers, and the streaming
        one catches it and sends an ``error`` event.
        """
        while True:
            item = await self._queue.get()
            if isinstance(item, Failed):
                raise item.error
            yield item
            if isinstance(item, Done):
                return


async def _run(events: AsyncIterator[Event], queue: asyncio.Queue[Item]) -> None:
    """Drive one turn to the end, whoever is listening. Always ends the queue.

    Every way out puts a last item on the queue, because a reader waiting on a queue
    nobody will write to again waits forever. ``put_nowait`` never blocks on an
    unbounded queue, which is also why the queue has no size limit: the producer
    must never wait on a reader that may have gone.

    In each ``except`` below, the queue comes first and the log line second. Code in
    an ``except`` block can fail too, and logging is code: a broken handler, or a
    character the console cannot encode, raises from inside the log call. Logged
    first, that failure would skip the ``put_nowait`` and leave the reader waiting
    for good.

    Cancellation arrives *inside* ``events`` -- this task is the one running it -- so
    a deadline or a shutdown unwinds the assistant from within, and the OpenAI stream
    is closed on the way out (llm/responses.py). Nothing here has to close it.
    """
    try:
        async with asyncio.timeout(TURN_DEADLINE_SECONDS):
            async for event in events:
                queue.put_nowait(event)
    except TimeoutError:
        queue.put_nowait(
            Failed(AssistantError(f"The answer took longer than {TURN_DEADLINE_SECONDS}s."))
        )
        log.warning("turn_timed_out", deadline_seconds=TURN_DEADLINE_SECONDS)
    except asyncio.CancelledError:
        # Only the shutdown in main.py cancels a turn. Tell any reader, then let the
        # cancellation carry on: swallowing it would make the task look finished
        # normally to the code that cancelled it.
        queue.put_nowait(Failed(AssistantError("The service is shutting down.")))
        raise
    except PortfolioAIError as exc:
        # A failure we raise on purpose, with a message written for a person.
        queue.put_nowait(Failed(exc))
        log.warning("turn_failed", error_type=type(exc).__name__, error=str(exc))
    except Exception as exc:
        # Anything else is a bug or the database. Handed on, so the visitor gets an
        # error rather than a stream that never ends, and logged with its traceback.
        queue.put_nowait(Failed(exc))
        log.exception("turn_failed", error_type=type(exc).__name__)


class Turns:
    """Every turn in flight: at most one per conversation, and all of them findable.

    Lives on ``app.state``, one per app, created with the app. Nothing here awaits
    except :meth:`drain`, and that is what makes the admission checks in ``deps.py``
    safe without a lock: asyncio switches between tasks only at an ``await``, so a
    "check, then start" with no await between them cannot be interleaved with
    another request doing the same.

    One process, so memory is the right place for this. Run two and each would
    allow one turn per conversation of its own; the fix then would be a lock in
    Postgres, not a bigger dictionary.
    """

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[None]] = {}
        # Set at shutdown, so a request arriving while turns drain is turned away
        # rather than started and cancelled a moment later.
        self.closing = False

    def busy(self, session_id: str) -> bool:
        """Whether this conversation has an answer still being written."""
        task = self._running.get(session_id)
        # A finished task can still be in the dict for a moment: done-callbacks run
        # on the next pass of the event loop, not the instant the task ends.
        return task is not None and not task.done()

    @property
    def in_flight(self) -> int:
        return sum(1 for task in self._running.values() if not task.done())

    def start(self, session_id: str, events: AsyncIterator[Event]) -> Turn:
        """Start running ``events`` in its own task and return a way to watch it."""
        if self.busy(session_id):
            # deps.py checks first, so reaching this is a bug, not a visitor.
            raise RuntimeError("a turn is already running for this conversation")

        queue: asyncio.Queue[Item] = asyncio.Queue()
        task = asyncio.create_task(_run(events, queue), name="turn")
        self._running[session_id] = task
        # partial() fixes the first argument now; the loop supplies the task later.
        task.add_done_callback(partial(self._forget, session_id))
        return Turn(session_id, queue)

    def _forget(self, session_id: str, task: asyncio.Task[None]) -> None:
        # Only if the entry is still this task. The next turn in the same
        # conversation may already have replaced it -- busy() treats a finished task
        # as free -- and removing that one would lift the limit mid-answer.
        if self._running.get(session_id) is task:
            del self._running[session_id]

    async def drain(self, grace_seconds: float) -> None:
        """Wait for turns in flight to finish, then cancel whatever is left.

        Called on shutdown, after the server has stopped taking requests. Turns
        still running here are mostly ones whose visitor already left; waiting a
        few seconds lets them be stored instead of lost.
        """
        tasks = [task for task in self._running.values() if not task.done()]
        if not tasks:
            return

        log.info("turns_draining", count=len(tasks), grace_seconds=grace_seconds)
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        # Wait for the cancellations to land. return_exceptions=True collects the
        # CancelledErrors as values instead of raising the first one.
        await asyncio.gather(*pending, return_exceptions=True)

        if pending:
            log.warning("turns_cancelled_at_shutdown", count=len(pending))
