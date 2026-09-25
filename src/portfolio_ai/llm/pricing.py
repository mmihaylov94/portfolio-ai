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

import re
from decimal import Decimal
from typing import NamedTuple

import structlog

log = structlog.get_logger(__name__)

# The API answers with the snapshot that actually ran: ask for "gpt-5-mini" and the
# response says "gpt-5-mini-2025-08-07". Found by the first live run, which priced
# every call at exactly $0 -- the table has the alias, the lookup had the snapshot,
# and an unknown model costs nothing by design (see cost_usd). A snapshot is billed
# as its alias, so the date comes off before the lookup.
_SNAPSHOT_DATE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


class ModelPrice(NamedTuple):
    """Dollars per million tokens, three ways.

    A NamedTuple rather than a bare tuple: it is still a tuple -- immutable, cheap,
    unpackable -- but ``price.cached_input`` says what it is at the point of use,
    where ``price[1]`` would need a comment every time.
    """

    input: Decimal
    # Input tokens the provider already had cached. OpenAI caches the longest
    # prefix it has seen recently once a prompt passes 1,024 tokens, and bills that
    # part at a tenth of the price. The Rachel prompt plus the tool description is
    # past that threshold, so on a knowledge-base answer this is most of the input
    # -- ignoring it would overstate the cost of every answer.
    cached_input: Decimal
    output: Decimal


# Verified 2026-09-22 against developers.openai.com/api/docs/pricing, standard tier.
#
# Embedding models bill one rate for input, have no cache discount and produce no
# output. `cached_input` repeats the input rate and `output` is zero rather than
# either being absent, which keeps every lookup the same shape.
PRICES: dict[str, ModelPrice] = {
    "text-embedding-3-small": ModelPrice(Decimal("0.02"), Decimal("0.02"), Decimal("0")),
    "text-embedding-3-large": ModelPrice(Decimal("0.13"), Decimal("0.13"), Decimal("0")),
    "gpt-5-mini": ModelPrice(Decimal("0.25"), Decimal("0.025"), Decimal("2.00")),
    "gpt-5": ModelPrice(Decimal("1.25"), Decimal("0.125"), Decimal("10.00")),
}

_PER_MILLION = Decimal(1_000_000)


def is_priced(model: str) -> bool:
    """Whether this table has a price for ``model``, dated snapshot names included.

    An eval run checks every model it will call against this before spending
    anything. A misspelt model name would otherwise fail on every call it makes, and
    an unpriced one would report its cost as exactly $0.
    """
    return model in PRICES or _SNAPSHOT_DATE.sub("", model) in PRICES


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int = 0,
    cached_tokens: int = 0,
) -> Decimal:
    """Cost of one call, in dollars.

    ``input_tokens`` is the whole input, cached part included -- that is how the API
    reports it -- and ``cached_tokens`` says how much of it was cached.

    An unknown model returns zero and logs a warning rather than raising. The
    reason is a judgement about which failure is worse: a model this table has not
    caught up with is a reporting gap, and stopping an ingestion run or an eval
    sweep over a missing price would turn a stale constant into an outage.

    The warning is what stops it being silent. A cost of exactly zero in a report
    is also its own signal -- real usage is never free.
    """
    price = PRICES.get(model) or PRICES.get(_SNAPSHOT_DATE.sub("", model))

    if price is None:
        log.warning("unknown_model_price", model=model, hint="add it to llm/pricing.py")
        return Decimal(0)

    uncached = max(0, input_tokens - cached_tokens)
    return (
        uncached * price.input + cached_tokens * price.cached_input + output_tokens * price.output
    ) / _PER_MILLION


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
