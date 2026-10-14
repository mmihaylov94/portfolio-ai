"""How many contact form messages the last hour has seen, the ones in hand now, and
who has been sent an automatic reply.

Three small jobs that each need to remember something between requests, kept on one
object that lives on ``app.state``, one per app, like the chat's ``Turns``. The third
is ``Replies`` (``replies.py``), which the desk only holds.

**A limit of its own.** A submission can cost two model calls and send two emails.
The daily spending limit counts chat answers and does not see either, and the
site's API limits each visitor, by IP address, and not the total. So the whole
service takes so many an hour (``CONTACT_MAX_PER_HOUR``) and refuses the rest. The count is kept in
memory, which is right for one process and forgets on a restart; a flood that
outlasts a restart is still bounded to the same number an hour.

**A submission in hand is finished, whoever is waiting.** The request that brought
it waits for the result, but the work runs in a task of its own. If the caller
gives up half-way, after the visitor has been sent an acknowledgement and before
the owner has been told, cancelling the request must not cancel the rest. The
endpoint awaits the task through ``asyncio.shield``, which takes the cancellation
itself and leaves the task running (``api/turns.py`` has the longer account of
why a task needs both a reference and a careful ``await``).
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import Coroutine
from typing import Any

import structlog

from portfolio_ai.contact.replies import Replies
from portfolio_ai.contact.service import Outcome

log = structlog.get_logger(__name__)

HOUR_SECONDS = 3600.0


class Desk:
    def __init__(self) -> None:
        # When each submission of the last hour arrived, oldest first. A deque,
        # because the oldest leave from the front as fast as new ones join the back,
        # and a list is slow at the front.
        self._arrivals: deque[float] = deque()
        self._in_hand: set[asyncio.Task[Outcome]] = set()
        # Set at shutdown, so a submission arriving while the last ones finish is
        # turned away rather than started and cancelled a moment later. A second line
        # of defence: uvicorn has stopped taking connections by the time this is set.
        self.closing = False
        # Who has been written to in the last day, and how many replies it has seen.
        self.replies = Replies()

    def admit(self, limit: int, *, now: float | None = None) -> int | None:
        """Count one submission in. None when it may go ahead; otherwise how many
        seconds until the next one would be let in.

        ``time.monotonic`` and not the wall clock: it only ever goes forwards, so a
        clock put right by the system cannot make an hour shorter or longer. ``now``
        is there for the tests.
        """
        moment = time.monotonic() if now is None else now
        while self._arrivals and moment - self._arrivals[0] >= HOUR_SECONDS:
            self._arrivals.popleft()
        if len(self._arrivals) >= limit:
            return math.ceil(HOUR_SECONDS - (moment - self._arrivals[0]))
        self._arrivals.append(moment)
        return None

    def start(self, work: Coroutine[Any, Any, Outcome]) -> asyncio.Task[Outcome]:
        """Run ``work`` in a task of its own, and keep hold of it until it is done.

        The event loop holds only a weak reference to a task, so one that nothing
        else refers to can be collected while it runs. The set is that reference.
        """
        task = asyncio.create_task(work, name="contact")
        self._in_hand.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Outcome]) -> None:
        self._in_hand.discard(task)
        if task.cancelled():
            return
        # Asking a finished task for its exception also tells asyncio somebody
        # looked: otherwise a failure nobody was waiting for is reported, much later
        # and without context, as "Task exception was never retrieved".
        error = task.exception()
        if error is not None:
            # The kind of failure, and nothing from the message it happened to.
            log.warning("contact_failed", error_type=type(error).__name__)

    @property
    def in_hand(self) -> int:
        return len(self._in_hand)

    async def drain(self, grace_seconds: float) -> None:
        """At shutdown: wait for submissions in hand, then cancel whatever is left."""
        tasks = list(self._in_hand)
        if not tasks:
            return
        log.info("contact_draining", count=len(tasks), grace_seconds=grace_seconds)
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if pending:
            log.warning("contact_cancelled_at_shutdown", count=len(pending))
