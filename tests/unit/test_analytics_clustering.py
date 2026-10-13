"""Grouping vectors, on arrows that can be drawn.

Every vector here is a direction on a flat page, given as an angle in degrees.
Cosine similarity between two of them is the cosine of the angle between them, so
the expected groups can be worked out with nothing but that: 60 degrees apart is
0.5 alike, 40 is 0.766, 90 is nothing.
"""

import math

import pytest

from portfolio_ai.analytics.clustering import centre_of, cluster


def at(degrees: float) -> list[float]:
    """The unit vector pointing ``degrees`` round from east."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)]


def angle(vector: list[float]) -> float:
    return math.degrees(math.atan2(vector[1], vector[0]))


# --- a centre -------------------------------------------------------------------


def test_a_centre_points_where_its_members_point_on_average() -> None:
    centre = centre_of([at(0), at(60)])

    assert angle(centre) == pytest.approx(30)
    assert math.hypot(*centre) == pytest.approx(1.0), "scaled to length 1"


def test_a_centre_is_a_direction_and_takes_no_notice_of_length() -> None:
    """Three arrows one way and one the other pull the centre three to one, however
    long any of them is drawn: the centre is rescaled, so only direction counts."""
    centre = centre_of([at(0), at(0), at(0), at(90)])

    assert angle(centre) == pytest.approx(math.degrees(math.atan2(1, 3)))
    assert math.hypot(*centre) == pytest.approx(1.0)


def test_the_centre_of_one_vector_is_that_vector() -> None:
    assert centre_of([at(40)]) == pytest.approx(at(40))


def test_arrows_that_cancel_have_no_direction_and_do_not_divide_by_nothing() -> None:
    assert centre_of([at(0), at(180)]) == pytest.approx([0.0, 0.0], abs=1e-12)


# --- groups ---------------------------------------------------------------------


def test_vectors_that_point_the_same_way_are_one_group_and_the_rest_another() -> None:
    groups = cluster([at(0), at(90), at(10), at(100)], threshold=0.9)

    assert [group.members for group in groups] == [[0, 2], [1, 3]]
    assert [round(angle(group.centre)) for group in groups] == [5, 95]


def test_a_vector_like_nothing_else_is_a_group_of_one() -> None:
    """One visitor asking about something the knowledge base lacks is still a gap."""
    groups = cluster([at(0), at(5), at(180)], threshold=0.9)

    assert [group.members for group in groups] == [[0, 1], [2]]


def test_the_largest_group_comes_first_and_ties_keep_their_order() -> None:
    groups = cluster([at(200), at(0), at(100), at(5), at(10), at(105)], threshold=0.9)

    assert [group.members for group in groups] == [[1, 3, 4], [2, 5], [0]]


def test_every_vector_is_in_exactly_one_group() -> None:
    vectors = [at(degrees) for degrees in range(0, 360, 7)]

    groups = cluster(vectors, threshold=0.8)

    placed = sorted(member for group in groups for member in group.members)
    assert placed == list(range(len(vectors)))


def test_the_threshold_is_met_by_being_exactly_as_alike_as_it_asks() -> None:
    alike = math.cos(math.radians(40))  # two arrows 40 degrees apart

    assert len(cluster([at(0), at(40)], threshold=alike - 1e-9)) == 1
    assert len(cluster([at(0), at(40)], threshold=alike + 1e-9)) == 2


def test_nothing_to_group_is_no_groups() -> None:
    assert cluster([], threshold=0.5) == []


def test_the_same_vectors_in_the_same_order_give_the_same_groups() -> None:
    """The result depends on the order, which is why the caller always hands the
    questions over in id order. Given that, it is repeatable."""
    vectors = [at(degrees) for degrees in (0, 50, 95, 20, 140, 70, 160)]

    first = [group.members for group in cluster(vectors, threshold=0.7)]
    second = [group.members for group in cluster(vectors, threshold=0.7)]

    assert first == second


# --- the second pass ------------------------------------------------------------
#
# Both tests place a vector early, at the edge of the first group, and then pull
# that group's centre away from it with later arrivals. At threshold 0.5 two
# vectors group when they are within 60 degrees.


def test_a_vector_moves_to_a_group_that_did_not_exist_when_it_was_placed() -> None:
    """60 joins 0, being exactly 60 degrees from it. Then -10 and -20 join and drag
    the centre to about 6 degrees, 54 from the early one; and 100 starts a second
    group, only 40 degrees from it. Placed again, 60 belongs with 100."""
    groups = cluster([at(0), at(60), at(-10), at(-20), at(100)], threshold=0.5)

    assert [group.members for group in groups] == [[0, 2, 3], [1, 4]]


def test_a_vector_its_group_drifted_away_from_ends_up_alone() -> None:
    """59 joins 0. Then -20, -30 and -40 pull the centre past -10, more than 60
    degrees from it, and there is no other group for it to go to."""
    groups = cluster([at(0), at(59), at(-20), at(-30), at(-40)], threshold=0.5)

    assert [group.members for group in groups] == [[0, 2, 3, 4], [1]]


def test_two_vectors_the_same_group_drifted_away_from_are_grouped_with_each_other() -> None:
    """Two identical vectors at 50 join 0. Five more, from -20 to -70, then pull the
    centre to about -20, some 70 degrees from the pair. Neither has another group
    to go to, and they are as alike as two vectors can be.

    The first version set each aside as a group of one without comparing them: two
    visitors who ran the very same search became two gaps. The strays of a round now
    get a round of their own.
    """
    groups = cluster(
        [at(0), at(50), at(50), at(-20), at(-35), at(-50), at(-60), at(-70)], threshold=0.5
    )

    assert [group.members for group in groups] == [[0, 3, 4, 5, 6, 7], [1, 2]]
    assert angle(groups[1].centre) == pytest.approx(50)


def test_two_vectors_left_behind_that_are_not_alike_stay_apart() -> None:
    """The "ends up alone" case twice over, the second a mirror image of the first:
    59 is left behind by the group around 0, and 121 by the group around 180. They
    are 62 degrees apart, a little more than the 60 that would group them. Giving
    the strays a round of their own must not mean lumping them together."""
    groups = cluster(
        [at(0), at(59), at(180), at(121), at(-20), at(-30), at(-40), at(200), at(210), at(220)],
        threshold=0.5,
    )

    assert [group.members for group in groups] == [[0, 4, 5, 6], [2, 7, 8, 9], [1], [3]]


def test_vectors_with_no_direction_are_each_a_group_and_the_grouping_still_ends() -> None:
    """A vector of zeros is like nothing, itself included, so no round can settle
    it. Embeddings of text are never that; a loop that only ends for well-behaved
    input is still a loop that can hang a scheduled job."""
    groups = cluster([[0.0, 0.0], at(10), [0.0, 0.0]], threshold=0.5)

    assert sorted(group.members for group in groups) == [[0], [1], [2]]


def test_without_the_second_pass_that_vector_would_have_stayed_where_it_was_put() -> None:
    """The check that the two tests above test the second pass and not the first:
    after one pass only, 60 is still grouped with 0."""
    vectors = [at(0), at(60), at(-10), at(-20), at(100)]

    # One pass, written out: each vector joins the nearest centre so far or starts one.
    groups: list[list[int]] = []
    for index, vector in enumerate(vectors):
        best, best_similarity = -1, -1.0
        for number, members in enumerate(groups):
            centre = centre_of([vectors[member] for member in members])
            similarity = math.sumprod(vector, centre)
            if similarity > best_similarity:
                best, best_similarity = number, similarity
        if best_similarity >= 0.5:
            groups[best].append(index)
        else:
            groups.append([index])

    assert groups == [[0, 1, 2, 3], [4]]
