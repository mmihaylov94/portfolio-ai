"""Grouping questions that are about the same thing, with arithmetic and nothing else.

A content gap is a group of questions the knowledge base could not answer that
would all be closed by one article. Finding the groups is clustering, and this is
the simplest kind that works on a few dozen points: no library, no number of groups
chosen in advance, about sixty lines.

**The one idea: a group is its centre.** Each question is an arrow in the embedding
space (``signals.py`` has that picture). A group is summed up by one arrow pointing
where its members point on average, its centre, and a question belongs to the group
whose centre it points most nearly along, provided that is near enough. "Near
enough" is one number, the threshold, and it is the only knob there is.

One round is two passes over the questions:

1. **Make the groups.** Take the questions in order. Each joins the nearest group if
   its centre is at least ``threshold`` alike, and the centre is then moved to take
   the newcomer into account. Otherwise the question starts a group of its own.
2. **Settle them.** The centres moved as members arrived, so an early question may
   now sit nearer a group that did not exist when it was placed. Every question is
   placed again, against the centres as they ended up.

A question that the second pass finds near no centre at all, because its own group
drifted away from it, is a stray. **The strays then get a round of their own**, and
theirs get another, until none are left. Setting each stray aside as a group of one
was the first version, and it made two gaps out of two identical questions: both had
been left behind by the same drifting group, and neither was ever compared with the
other.

The usual tools for this (k-means, agglomerative clustering) live in scikit-learn,
which would be the largest dependency in the project, brought in to group twenty
sentences. k-means also needs the number of groups in advance, which is the one
thing not known here. The cost of doing it by hand is that the result depends on
the order of the questions; they are always handed over in id order, so the same
questions give the same groups.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

# A sum of unit vectors shorter than this has no direction worth the name.
_NO_DIRECTION = 1e-9


@dataclass(frozen=True)
class Cluster:
    """One group: which of the vectors handed in belong to it, and its centre."""

    # Positions in the sequence that was clustered, in that sequence's order.
    members: list[int]
    # The members' average direction, at length 1.
    centre: list[float]


def _unit(vector: Sequence[float]) -> list[float]:
    """``vector`` scaled to length 1, or all zeros when it has no direction.

    Arrows that cancel leave a sum of rounding error, a length of about 1e-16, and
    scaling that up to 1 would invent a direction out of noise. Embeddings of real
    text never cancel; the hand-made vectors in the tests can.
    """
    length = math.sqrt(math.sumprod(vector, vector))
    if length < _NO_DIRECTION:
        return [0.0] * len(vector)
    return [value / length for value in vector]


def centre_of(vectors: Sequence[Sequence[float]]) -> list[float]:
    """The average direction of ``vectors``, as a vector of length 1.

    The members are added up, coordinate by coordinate, and the sum is scaled to
    length 1. ``zip(*vectors)`` is the idiom that turns a list of rows into its
    columns: it hands back all the first coordinates, then all the second ones.
    Scaling to length 1 is what lets a centre be compared with a question by a dot
    product as though it were one more question.
    """
    return _unit([math.fsum(column) for column in zip(*vectors, strict=True)])


def _nearest(vector: Sequence[float], centres: Sequence[Sequence[float]]) -> tuple[int, float]:
    """Which centre ``vector`` is most alike, and how alike. (-1, -1.0) when there are none.

    A dot product and not a cosine: both sides are already at length 1, so the two
    are the same number, and this is the line that runs most often.
    """
    best, best_similarity = -1, -1.0
    for index, centre in enumerate(centres):
        similarity = math.sumprod(vector, centre)
        if similarity > best_similarity:
            best, best_similarity = index, similarity
    return best, best_similarity


def _round(
    indexes: Sequence[int], units: Sequence[Sequence[float]], threshold: float
) -> tuple[list[list[int]], list[int]]:
    """One round over ``indexes``: the groups that settled, and the strays.

    A group's centre is kept as a running sum of its members, scaled to length 1
    after each arrival. Adding one vector to a sum costs as much as the vector is
    long; working the centre out afresh from every member, each time one joined,
    cost that times the size of the group.
    """
    # --- pass 1: make the groups ---------------------------------------------
    sums: list[list[float]] = []
    centres: list[list[float]] = []
    for index in indexes:
        nearest, similarity = _nearest(units[index], centres)
        if similarity >= threshold:
            sums[nearest] = [a + b for a, b in zip(sums[nearest], units[index], strict=True)]
            centres[nearest] = _unit(sums[nearest])
        else:
            sums.append(list(units[index]))
            centres.append(list(units[index]))

    # --- pass 2: settle them -------------------------------------------------
    settled: list[list[int]] = [[] for _ in centres]
    strays: list[int] = []
    for index in indexes:
        nearest, similarity = _nearest(units[index], centres)
        if similarity >= threshold:
            settled[nearest].append(index)
        else:
            # Its group's centre moved away from it as others joined.
            strays.append(index)

    return [members for members in settled if members], strays


def cluster(vectors: Sequence[Sequence[float]], *, threshold: float) -> list[Cluster]:
    """Group ``vectors``. Largest group first; groups of one are groups too.

    Every vector ends up in exactly one group. A question nothing else resembles is
    a group of one, which is right: one visitor asking about something the knowledge
    base lacks is still a gap.
    """
    # Length 1 once, here, so that everything after is a dot product. OpenAI's
    # embeddings arrive at length 1 already; the tests' vectors may not.
    units = [_unit(vector) for vector in vectors]

    groups: list[list[int]] = []
    pending = list(range(len(units)))
    while pending:
        settled, strays = _round(pending, units, threshold)
        if not settled:
            # Nothing found a centre it was near enough to, its own included, so
            # another round would go the same way. That takes a vector with no
            # direction, which is like nothing. Each is a group of its own.
            groups += [[index] for index in strays]
            break
        groups += settled
        # A round that settled anything leaves fewer for the next, so this ends.
        # In practice a round always settles something: the last vector it placed
        # joined a centre that was then moved towards it, and by nothing after it.
        pending = strays

    found = [
        Cluster(members=members, centre=centre_of([units[member] for member in members]))
        for members in groups
    ]
    # sorted() is stable, so groups of equal size keep the order they were settled
    # in: a round's groups in the order of their first questions, and the groups
    # made of its strays after them.
    return sorted(found, key=lambda group: len(group.members), reverse=True)
