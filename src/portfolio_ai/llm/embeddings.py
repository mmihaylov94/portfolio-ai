"""Turning text into vectors, in batches, with the cost written down.

One call per batch rather than one per chunk. The corpus is around 111 chunks, so
this is one or two requests instead of 111 -- and since every request pays the
same round trip to California, that difference is most of the runtime.

Batches are sent one after another rather than concurrently. It would be easy to
fan them out with ``gather_limited``, and at this size it would save perhaps a
second while making a rate limit much easier to hit. The fetches from GitHub are
concurrent because there are eleven of them and they are free; embedding requests
are few, expensive and rate-limited, which is the opposite trade.
"""

import time
from dataclasses import dataclass
from decimal import Decimal

import structlog
from openai import AuthenticationError, PermissionDeniedError

from portfolio_ai.config import get_settings
from portfolio_ai.exceptions import ConfigError, EmbeddingError
from portfolio_ai.llm.client import get_client
from portfolio_ai.llm.pricing import cost_usd

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    """Vectors, plus what they cost to produce.

    The usage figures come from the API response rather than from counting tokens
    here, which makes them exact instead of estimated. They travel with the
    vectors because the caller reporting on a run needs both, and threading them
    back separately would mean a second return value everybody has to remember.
    """

    vectors: list[list[float]]
    model: str
    total_tokens: int
    cost: Decimal
    duration_ms: int


async def embed_texts(texts: list[str]) -> EmbeddingResult:
    """Embed every string, in order.

    ``vectors[i]`` belongs to ``texts[i]``. That is guaranteed by the API -- the
    response carries an ``index`` on each item -- but it is only true if you sort
    by it, because the items are not promised to arrive in order. See below.
    """
    settings = get_settings()

    if not texts:
        return EmbeddingResult([], settings.embedding_model, 0, Decimal(0), 0)

    client = get_client()
    started = time.perf_counter()

    vectors: list[list[float]] = []
    total_tokens = 0

    for start in range(0, len(texts), settings.embedding_batch_size):
        batch = texts[start : start + settings.embedding_batch_size]

        try:
            response = await client.embeddings.create(
                input=batch,
                model=settings.embedding_model,
                # Asked for explicitly even though 1536 is this model's default.
                # The number is recorded in the migration as vector(1536), and a
                # silent change to the default would produce vectors the column
                # rejects -- which is the good outcome. Stating it means the two
                # agree on purpose.
                dimensions=settings.embedding_dimensions,
            )
        except (AuthenticationError, PermissionDeniedError) as exc:
            # A rejected key is a configuration problem, not a bug, so it gets a
            # readable one-line refusal and exit code 1 rather than forty lines of
            # SDK traceback ending in a 401. Everything else from the SDK -- rate
            # limits it gave up on, a bad request, a network failure -- keeps its
            # traceback, because those are either transient or genuinely ours.
            raise ConfigError(
                f"OpenAI rejected the API key. Check OPENAI_API_KEY in .env ({type(exc).__name__})."
            ) from exc

        # Sort by index before taking the vectors. The API documents that items
        # may come back in any order, and relying on the order they happen to
        # arrive in would mismatch chunks to embeddings -- a failure with no error
        # and no symptom except that retrieval quietly returns the wrong section.
        ordered = sorted(response.data, key=lambda item: item.index)

        if len(ordered) != len(batch):
            raise EmbeddingError(f"asked for {len(batch)} embeddings, got {len(ordered)} back")

        for item in ordered:
            if len(item.embedding) != settings.embedding_dimensions:
                raise EmbeddingError(
                    f"expected {settings.embedding_dimensions}-dimensional vectors, "
                    f"got {len(item.embedding)}"
                )
            vectors.append(item.embedding)

        total_tokens += response.usage.total_tokens

    duration_ms = int((time.perf_counter() - started) * 1000)
    cost = cost_usd(settings.embedding_model, total_tokens)

    log.info(
        "embeddings_created",
        count=len(vectors),
        model=settings.embedding_model,
        total_tokens=total_tokens,
        cost_usd=str(cost),
        duration_ms=duration_ms,
    )

    return EmbeddingResult(
        vectors=vectors,
        model=settings.embedding_model,
        total_tokens=total_tokens,
        cost=cost,
        duration_ms=duration_ms,
    )
