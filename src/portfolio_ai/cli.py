"""Shared plumbing for the project's command-line entry points.

Ingestion and the analytics digest both run as ``python -m portfolio_ai.something``
from a cron schedule inside a container. They need the same three things every
time: logging configured before anything happens, an event loop started, and
failures turned into an exit code the scheduler can act on rather than a traceback
nobody will read.

That is all this module is.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import structlog

from portfolio_ai.exceptions import PortfolioAIError
from portfolio_ai.logging import configure_logging

log = structlog.get_logger(__name__)

# Conventionally, a program killed by signal N exits with 128 + N. Ctrl-C sends
# SIGINT, which is 2. Shells and schedulers understand this, so it is worth using
# the convention rather than inventing a number.
_EXIT_INTERRUPTED = 130
_EXIT_FAILED = 1


def run_async(main: Callable[[], Coroutine[Any, Any, None]]) -> int:
    """Run an async ``main`` as a program, and return the exit code.

    Note what gets passed in: the function itself, **not** the result of calling
    it. ``run_async(main)``, never ``run_async(main())``.

    The reason is that calling an async function does not run it -- it builds a
    coroutine object that sits there waiting to be awaited. If we accepted that
    object and then something failed before the event loop started, the coroutine
    would be discarded unawaited and Python would print::

        RuntimeWarning: coroutine 'main' was never awaited

    on the way out, muddying a perfectly clear error. Taking the function instead
    means nothing is created until we are ready to run it.

    Usage::

        if __name__ == "__main__":
            raise SystemExit(run_async(main))
    """
    try:
        configure_logging()
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl-C is a decision, not a malfunction. A twenty-line traceback showing
        # where in the event loop the interrupt landed helps nobody.
        log.warning("interrupted")
        return _EXIT_INTERRUPTED
    except PortfolioAIError as exc:
        # Something we rejected on purpose: bad configuration, a refused purge.
        # The message is already written for a human, so log it and leave. Other
        # exceptions are bugs and deserve their traceback, so they are not caught.
        log.error("command_failed", error=str(exc), error_type=type(exc).__name__)
        return _EXIT_FAILED
    return 0
