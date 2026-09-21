"""Running many awaitables at once, with a ceiling on how many.

``asyncio.gather`` starts everything at the same moment, which is exactly what you
want for ten things and exactly what you do not want for a thousand. Ingestion
fetches every Markdown file in the knowledge base; the assistant will embed batches
of chunks. In both cases "all at once" means opening as many connections as there
are items, and getting rate-limited or refused for the trouble.

So the helper here is ``gather`` with a cap.
"""

import asyncio
from collections.abc import Awaitable


async def gather_limited[T](limit: int, *awaitables: Awaitable[T]) -> list[T]:
    """Await all of them, never running more than ``limit`` at the same time.

    Results come back in the order the awaitables were passed in, not the order
    they happened to finish. That is worth knowing: it means you can zip the
    results back against whatever you built them from without tracking identities.

        urls = [...]
        bodies = await gather_limited(5, *(fetch(u) for u in urls))
        # bodies[i] belongs to urls[i], regardless of which returned first

    If one of them raises, that exception surfaces here. The others are *not*
    cancelled and keep running to completion in the background -- a sharp edge in
    ``asyncio.gather`` itself, worth remembering before relying on an early failure
    to stop the rest of the work.

    The ``[T]`` after the name says: whatever type these awaitables produce, that is
    the type of the list coming back. It buys nothing at runtime and lets a type
    checker follow the values through, which lesson 9 has more to say about.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")

    semaphore = asyncio.Semaphore(limit)

    async def guarded(awaitable: Awaitable[T]) -> T:
        # Waits here if `limit` others are already inside, and releases on the way
        # out -- including if the awaitable raises, which is the reason to use
        # `async with` rather than acquire/release by hand.
        async with semaphore:
            return await awaitable

    return await asyncio.gather(*(guarded(a) for a in awaitables))
