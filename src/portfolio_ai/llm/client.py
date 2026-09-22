"""The OpenAI client, created once and shared.

Same shape as ``db/pool.py``, and for the same reason: the client holds a pooled
HTTP connection to api.openai.com, and building a new one per call would mean a
fresh TLS handshake every time. It is cached at module level and reset explicitly.

There is no wrapper class around the SDK. Retries, timeouts and backoff are
already the SDK's job and it does them well; adding a layer would mean either
duplicating that or hiding it. What this module does is make sure the client is
configured from ``Settings`` rather than from ``OPENAI_API_KEY`` being read out of
the environment somewhere unpredictable.
"""

from functools import lru_cache

from openai import AsyncOpenAI

from portfolio_ai.config import get_settings


@lru_cache
def get_client() -> AsyncOpenAI:
    """Return the shared client, building it on first call.

    ``lru_cache`` on a function with no arguments is the lazy singleton from
    lesson 3. Lazy matters here: building the client reads settings, and settings
    reads the environment, so doing it at import would make importing this module
    fail on a machine with no configuration -- including during test collection.

    ``get_client.cache_clear()`` drops it, which is how tests get a fresh one.
    """
    settings = get_settings()

    return AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        # Without an explicit timeout the SDK waits ten minutes. For a scheduled
        # job that means a hung call is indistinguishable from a slow one until
        # long after anyone would have wanted to know.
        timeout=settings.openai_timeout_seconds,
        # The SDK retries connection errors, timeouts, 429s and 5xx with
        # exponential backoff and honours Retry-After. Anything else -- a bad
        # request, a bad key -- fails immediately, which is right: retrying a 400
        # just produces the same 400 more slowly.
        max_retries=settings.openai_max_retries,
    )


async def close_client() -> None:
    """Close the client and forget it. Safe when nothing was ever built.

    Worth calling at the end of a CLI run. Without it the process can sit holding
    an open connection while the event loop shuts down underneath it, which
    produces a warning about an unclosed session that looks like a bug and is not.
    """
    if get_client.cache_info().currsize:
        await get_client().close()
        get_client.cache_clear()
