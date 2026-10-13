"""The arithmetic behind the report, on numbers small enough to check by eye.

Vectors here have two or three dimensions instead of 1,536. Cosine similarity does
not care how many there are, and with two the right answer can be read off a
sketch: (1, 0) and (0, 1) are at right angles, (1, 0) and (1, 1) are 45 degrees
apart, and the cosine of 45 degrees is 0.707.
"""

import datetime as dt

import pytest
from turns import turn

from portfolio_ai.analytics.signals import (
    REPHRASE_WINDOW,
    consecutive_pairs,
    cosine,
    histogram,
    spread,
)

EAST = [1.0, 0.0]
NORTH = [0.0, 1.0]
NORTH_EAST = [1.0, 1.0]


# --- cosine ---------------------------------------------------------------------


def test_the_same_direction_scores_one_and_a_right_angle_scores_nothing() -> None:
    assert cosine(EAST, EAST) == pytest.approx(1.0)
    assert cosine(EAST, NORTH) == pytest.approx(0.0)
    assert cosine(EAST, [-1.0, 0.0]) == pytest.approx(-1.0)


def test_it_measures_direction_and_not_length() -> None:
    """Which is the point of dividing by the lengths: (1, 1) and (5, 5) point the
    same way, and a longer vector is not a more similar one."""
    assert cosine(EAST, NORTH_EAST) == pytest.approx(0.7071, abs=1e-4)
    assert cosine(EAST, [5.0, 5.0]) == pytest.approx(0.7071, abs=1e-4)


def test_a_vector_of_zeros_is_like_nothing_rather_than_a_division_by_zero() -> None:
    assert cosine([0.0, 0.0], EAST) == pytest.approx(0.0)


# --- questions asked one after the other ----------------------------------------


def test_each_question_is_paired_with_the_one_before_it() -> None:
    turns = [
        turn(1, "day rate?", minutes=0),
        turn(3, "what does he charge per day?", minutes=1),
        turn(5, "where is he based?", minutes=2),
    ]
    vectors = {
        "day rate?": EAST,
        "what does he charge per day?": NORTH_EAST,
        "where is he based?": NORTH,
    }

    pairs = consecutive_pairs(turns, vectors)

    assert [(pair.first.question_id, pair.second.question_id) for pair in pairs] == [(1, 3), (3, 5)]
    assert [pair.similarity for pair in pairs] == pytest.approx([0.7071, 0.7071], abs=1e-4)


def test_questions_in_different_conversations_are_never_paired() -> None:
    """Two visitors asking the same thing a minute apart are two visitors."""
    turns = [turn(1, "same", conversation=1), turn(3, "same", conversation=2, minutes=1)]

    assert consecutive_pairs(turns, {"same": EAST}) == []


def test_the_turns_may_arrive_in_any_order() -> None:
    """groupby only groups neighbours, so the function sorts first. Handed turns
    out of order, an unsorted version would see conversation 1 twice and pair
    nothing."""
    turns = [
        turn(3, "b", conversation=1, minutes=1),
        turn(7, "c", conversation=2, minutes=0),
        turn(1, "a", conversation=1, minutes=0),
    ]

    pairs = consecutive_pairs(turns, {"a": EAST, "b": EAST, "c": EAST})

    assert [(pair.first.question, pair.second.question) for pair in pairs] == [("a", "b")]


@pytest.mark.parametrize("small_talk_is", ["first", "second"])
def test_small_talk_on_either_side_is_not_a_pair(small_talk_is: str) -> None:
    """ "thanks" and "thanks a lot" are alike, and neither is a question put again."""
    first_route = "small_talk" if small_talk_is == "first" else "mihail_related"
    second_route = "small_talk" if small_talk_is == "second" else "mihail_related"
    turns = [
        turn(1, "a", route=first_route),
        turn(3, "b", route=second_route, minutes=1),
    ]

    # Small talk has no vector either: the report never embeds it. A lookup that
    # happened before the route was checked would raise KeyError here.
    vectors = {
        question: EAST
        for question, route in (("a", first_route), ("b", second_route))
        if route != "small_talk"
    }

    assert consecutive_pairs(turns, vectors) == []


def test_a_refused_question_asked_again_is_a_pair() -> None:
    """Off-topic is not small talk. A visitor rewording a question that was turned
    away is exactly what the signal is for: it may have been turned away wrongly."""
    turns = [turn(1, "a", route="out_of_scope"), turn(3, "b", minutes=1)]

    assert len(consecutive_pairs(turns, {"a": EAST, "b": EAST})) == 1


def test_questions_too_far_apart_are_not_a_pair() -> None:
    minutes = REPHRASE_WINDOW / dt.timedelta(minutes=1)
    inside = [turn(1, "a"), turn(3, "b", minutes=minutes)]
    outside = [turn(1, "a"), turn(3, "b", minutes=minutes + 1)]
    vectors = {"a": EAST, "b": EAST}

    assert len(consecutive_pairs(inside, vectors)) == 1, "exactly the window apart still counts"
    assert consecutive_pairs(outside, vectors) == []


def test_the_window_can_be_given() -> None:
    turns = [turn(1, "a"), turn(3, "b", minutes=3)]

    assert consecutive_pairs(turns, {"a": EAST, "b": EAST}, window=dt.timedelta(minutes=2)) == []


# --- timings --------------------------------------------------------------------


def test_nothing_measured_is_none_and_not_a_zero() -> None:
    """A median of 0.0s would read as "instant". There was nothing to time."""
    assert spread([]) is None


def test_one_value_is_its_own_median_and_slow_end() -> None:
    result = spread([3200.0])

    assert result is not None
    assert (result.count, result.median, result.p90) == (1, 3200.0, 3200.0)


def test_the_slow_end_is_never_slower_than_the_slowest() -> None:
    """The reason for method="inclusive". The default treats the values as a sample
    and extrapolates: for 1, 2 and 3 it reports a 90th percentile of 3.6."""
    result = spread([1.0, 2.0, 3.0])

    assert result is not None
    assert result.median == pytest.approx(2.0)
    assert result.p90 == pytest.approx(2.8)


def test_nine_in_ten_of_eleven_even_steps() -> None:
    result = spread([float(value) for value in range(11)])  # 0, 1, ... 10

    assert result is not None
    assert result.count == 11
    assert result.median == pytest.approx(5.0)
    assert result.p90 == pytest.approx(9.0)


# --- counting by band -----------------------------------------------------------


def test_values_are_counted_by_band_lowest_first() -> None:
    bands = histogram([0.41, 0.12, 0.44, 0.13, 0.29], width=0.05)

    assert [(round(low, 2), count) for low, count in bands] == [(0.10, 2), (0.25, 1), (0.40, 2)]


def test_a_value_on_an_edge_belongs_to_the_band_it_starts() -> None:
    """0.3 / 0.05 is 5.999999999999999 in floating point. Floored as it stands, 0.30
    would be counted under "0.25 to 0.30", on the wrong side of the threshold the
    report prints beside it."""
    [(low, count)] = histogram([0.30], width=0.05)

    assert (round(low, 2), count) == (0.30, 1)


def test_no_values_is_no_bands() -> None:
    assert histogram([], width=0.05) == []
