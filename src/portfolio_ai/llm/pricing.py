"""What an OpenAI call cost, in dollars.

A small hand-maintained table rather than anything clever. There is no API that
reports prices, so this is copied from the pricing page and has to be updated by
hand when it changes -- which is a real maintenance cost and still cheaper than
not knowing what the system spends.

Cost visibility is a feature here, not an afterthought. Every OpenAI call in this
project records its tokens and model, and this is what turns those numbers into
something you can compare between two eval runs or notice on a bill.

Money is ``Decimal``, never ``float``. Prices are decimal fractions and float
arithmetic on them accumulates error that shows up as totals that do not add up --
the kind of bug that is never worth the hour it takes to find.
"""

from decimal import Decimal

import structlog

log = structlog.get_logger(__name__)

# Dollars per million tokens. Verified 2026-09-22.
#
# Embedding models bill one rate for input and produce no output, so `output` is
# zero for them rather than absent -- it keeps every lookup the same shape.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    # model: (input $/1M, output $/1M)
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0")),
    "text-embedding-3-large": (Decimal("0.13"), Decimal("0")),
    "gpt-5-mini": (Decimal("0.25"), Decimal("2.00")),
    "gpt-5": (Decimal("1.25"), Decimal("10.00")),
}

_PER_MILLION = Decimal(1_000_000)


def cost_usd(model: str, input_tokens: int, output_tokens: int = 0) -> Decimal:
    """Cost of one call, in dollars.

    An unknown model returns zero and logs a warning rather than raising. The
    reason is a judgement about which failure is worse: a model this table has not
    caught up with is a reporting gap, and stopping an ingestion run or an eval
    sweep over a missing price would turn a stale constant into an outage.

    The warning is what stops it being silent. A cost of exactly zero in a report
    is also its own signal -- real usage is never free.
    """
    price = PRICES.get(model)

    if price is None:
        log.warning("unknown_model_price", model=model, hint="add it to llm/pricing.py")
        return Decimal(0)

    input_rate, output_rate = price
    return (input_tokens * input_rate + output_tokens * output_rate) / _PER_MILLION


# Roughly four characters per token for English prose. Used only where an estimate
# is honest -- planning a dry run, and the oversize guard in chunking -- never for
# reporting what a run actually spent, which comes from the API's own usage figures.
#
# The ratio is a rule of thumb and it is wrong for code, for other languages and for
# unusual punctuation. It is used here with margins of several times over, so being
# 30% out changes nothing.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate token count, for planning rather than billing."""
    return max(1, len(text) // CHARS_PER_TOKEN)
