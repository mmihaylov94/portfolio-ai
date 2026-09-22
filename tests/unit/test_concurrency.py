"""gather_limited: ordering, limits, and the guard on the limit itself.

These are async tests, and there is no decorator on them. ``asyncio_mode = "auto"``
in pyproject.toml means pytest awaits any async test it finds. Without that
setting an async test needs ``@pytest.mark.asyncio``, and forgetting it does not
fail -- pytest builds the coroutine, never awaits it, and reports a pass on a
test that never ran.
"""

import asyncio
import time

import pytest

from portfolio_ai.concurrency import gather_limited


async def _double(value: int, delay: float = 0.0) -> int:
    await asyncio.sleep(delay)
    return value * 2


async def test_results_come_back_in_argument_order() -> None:
    """Not completion order, which is the part worth testing.

    The delays are deliberately reversed, so the last one finishes first. If the
    results came back in completion order this would fail.
    """
    results = await gather_limited(3, *(_double(i, delay=(5 - i) * 0.01) for i in range(5)))

    assert results == [0, 2, 4, 6, 8]


async def test_everything_runs() -> None:
    results = await gather_limited(2, *(_double(i) for i in range(10)))

    assert len(results) == 10
    assert sum(results) == 90


async def test_the_limit_is_actually_applied() -> None:
    """Ten tasks of 0.05s with a limit of two cannot finish in under 0.25s.

    A timing assertion, which is normally a bad idea -- they fail on slow
    machines for reasons unrelated to the code. This one is a lower bound rather
    than an upper one: no amount of slowness can make it finish *too early*, so
    it cannot become flaky in the usual direction.
    """
    start = time.perf_counter()
    await gather_limited(2, *(_double(i, delay=0.05) for i in range(10)))
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.25


async def test_an_empty_call_is_fine() -> None:
    assert await gather_limited(3) == []


@pytest.mark.parametrize("limit", [0, -1])
async def test_a_meaningless_limit_is_rejected(limit: int) -> None:
    # No awaitable passed, deliberately. The first version of this test read
    # `gather_limited(limit, _double(1))`, which raised before ever awaiting that
    # coroutine -- so the test passed while emitting:
    #
    #     RuntimeWarning: coroutine '_double' was never awaited
    #
    # Lesson 5's point, arriving uninvited: calling an async function creates
    # something that has to be awaited or explicitly discarded. The limit check
    # runs before anything else, so nothing needs creating at all.
    with pytest.raises(ValueError, match="at least 1"):
        await gather_limited(limit)
