"""The cost table, and what it does when it does not know a model."""

from decimal import Decimal

from portfolio_ai.llm.pricing import PRICES, cost_usd, estimate_tokens


def test_cost_is_exact_decimal_arithmetic() -> None:
    """Not float. A million tokens of the embedding model is exactly two cents,
    and a total that is off by 1e-17 is a total nobody trusts."""
    assert cost_usd("text-embedding-3-small", 1_000_000) == Decimal("0.02")


def test_a_realistic_ingestion_run_costs_almost_nothing() -> None:
    """The whole corpus is roughly 14_400 tokens. Worth pinning: it is the number
    that makes 'just re-embed everything' the right answer nearly always."""
    assert cost_usd("text-embedding-3-small", 14_400) < Decimal("0.001")


def test_chat_models_bill_input_and_output_separately() -> None:
    assert cost_usd("gpt-5-mini", 1_000_000, 1_000_000) == Decimal("2.25")


def test_a_dated_snapshot_is_priced_as_its_alias() -> None:
    """Responses name the snapshot that ran, not the alias that was requested. The
    first live call reported gpt-5-mini-2025-08-07 and was priced at $0."""
    assert cost_usd("gpt-5-mini-2025-08-07", 1_000_000) == cost_usd("gpt-5-mini", 1_000_000)
    assert cost_usd("gpt-5-mini-2025-08-07", 1_000_000) > Decimal(0)


def test_cached_input_is_billed_at_the_cached_rate() -> None:
    """A million input tokens of which 800k were cached: 200k at $0.25/M plus
    800k at $0.025/M. The API counts cached tokens inside input_tokens, so they
    must be subtracted, not added."""
    assert cost_usd("gpt-5-mini", 1_000_000, 0, cached_tokens=800_000) == Decimal("0.07")


def test_cached_tokens_never_make_the_uncached_part_negative() -> None:
    # Cannot happen with real API figures. If it ever did, a negative term would
    # quietly shrink the total, which is the wrong direction for a cost to be wrong in.
    assert cost_usd("gpt-5-mini", 100, 0, cached_tokens=500) >= Decimal(0)


def test_an_unknown_model_returns_zero_rather_than_raising() -> None:
    """A price this table has not caught up with is a reporting gap. Failing an
    ingestion run or an eval sweep over a stale constant would turn it into an
    outage, so it warns and continues -- and a cost of exactly zero in a report is
    its own signal, because real usage is never free."""
    assert cost_usd("some-model-released-next-week", 1_000_000) == Decimal(0)


def test_embedding_models_have_no_output_price() -> None:
    for model, price in PRICES.items():
        if model.startswith("text-embedding"):
            assert price.output == Decimal(0)


def test_estimate_tokens_never_returns_zero_for_real_text() -> None:
    """A chunk estimated at zero tokens would slip past the oversize guard and,
    more importantly, make a dry run report a cost of nothing for real work."""
    assert estimate_tokens("hi") >= 1
    assert estimate_tokens("") >= 1
