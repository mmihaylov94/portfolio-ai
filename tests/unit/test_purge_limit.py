"""How many documents one ingestion run is allowed to delete.

Pure arithmetic, and worth testing on its own because it is the whole of the
safety property: everything else about the purge is a `delete` statement.
"""

import pytest

from portfolio_ai.db.documents import purge_limit

# The shipped defaults, so these cases describe real behaviour rather than an
# arrangement chosen to make the numbers come out.
FRACTION = 0.3
GRACE = 2


@pytest.mark.parametrize(
    ("stored", "expected", "because"),
    [
        (11, 4, "the current corpus: a tidy-up of three passes, a collapse does not"),
        (50, 15, "the case a fixed floor misses -- it scales with the corpus"),
        (500, 150, "and keeps scaling"),
        (3, 2, "the grace, not the share: a third of three would freeze a tiny corpus"),
        (1, 2, "removing the only article is allowed"),
        (0, 2, "a first run, where there is nothing to delete anyway"),
    ],
)
def test_limit_scales_with_what_is_stored(stored: int, expected: int, because: str) -> None:
    assert purge_limit(stored, FRACTION, GRACE) == expected, because


def test_the_old_fixed_floor_would_have_missed_this() -> None:
    """The reason for the change, stated as a test.

    A fixed floor of eight documents was a real guard over a corpus of eleven. Over
    fifty it is no guard at all: discovery collapsing to nine would clear
    forty-one and still pass, because nine is more than eight. The danger was
    always proportional to the corpus, so the limit has to be too.
    """
    stored = 50
    surviving_after_a_collapse = 9
    deletions = stored - surviving_after_a_collapse

    assert deletions > purge_limit(stored, FRACTION, GRACE), "refused, as it should be"
    assert surviving_after_a_collapse > 8, "yet it would have passed a fixed floor of 8"


def test_rounding_is_generous_rather_than_strict() -> None:
    """ceil, not floor. At eleven documents a third is 3.3, and rounding down
    would refuse a deletion of four that the operator almost certainly meant."""
    assert purge_limit(11, 0.3, 0) == 4


def test_a_fraction_of_zero_allows_only_the_grace() -> None:
    """Which is how you would pin the guard shut for a run you do not trust."""
    assert purge_limit(100, 0.0, 0) == 0
    assert purge_limit(100, 0.0, 2) == 2


def test_a_fraction_of_one_allows_everything() -> None:
    """The escape hatch: set INGESTION_MAX_PURGE_FRACTION=1.0 for the one run where
    you genuinely are deleting most of the knowledge base."""
    assert purge_limit(11, 1.0, 0) == 11
