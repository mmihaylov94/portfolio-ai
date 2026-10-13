"""What the stored turns say, worked out in plain Python.

Nothing in this module touches a database or a model. It is handed turns that
``db/analytics.py`` read and vectors that the embeddings call made, and it does
arithmetic on them, which is what makes it testable with numbers typed by hand.

The one idea worth carrying out of here: **an embedding turns "do these two
sentences mean the same thing" into "how small is the angle between two arrows".**
The model places every text at a point in a space of 1,536 dimensions, and texts
that mean similar things land in similar directions. The cosine of the angle
between two of them is 1 when they point the same way and near 0 when they have
nothing to do with each other. Retrieval uses that to find passages for a question
(``db/documents.search_chunks``, in SQL). This module uses it to notice a visitor
asking one thing twice.
"""

import datetime as dt
import itertools
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from portfolio_ai.db.analytics import Turn

# How close together two questions have to be asked to count as one being put
# again. Longer than a visitor takes to read an answer and try other words; shorter
# than a visitor who came back after lunch with a different question.
REPHRASE_WINDOW = dt.timedelta(minutes=10)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """How alike two vectors point: 1 the same way, 0 unrelated.

    The dot product of the two, divided by their lengths. ``math.sumprod`` is new in
    Python 3.12 and does the multiply-and-add in C: on two lists of 1,536 floats it
    took 26 microseconds here, against 109 for the same thing written as
    ``sum(x * y for x, y in zip(a, b))``, which is a loop run by the interpreter.
    One cosine is three of those, so a few hundred comparisons take a few hundredths
    of a second, which is why this project adds no numpy for them.

    OpenAI's embeddings already have length 1, which would make the division
    unnecessary. It stays, because a function called cosine should be right for
    any two vectors, the hand-made ones in the tests included.
    """
    lengths = math.sqrt(math.sumprod(a, a) * math.sumprod(b, b))
    return math.sumprod(a, b) / lengths if lengths else 0.0


@dataclass(frozen=True)
class Pair:
    """Two questions asked one after the other in a conversation."""

    first: Turn
    second: Turn
    similarity: float


def consecutive_pairs(
    turns: Iterable[Turn],
    vectors: Mapping[str, Sequence[float]],
    *,
    window: dt.timedelta = REPHRASE_WINDOW,
) -> list[Pair]:
    """Each question paired with the one before it in the same conversation.

    Every such pair is returned with its similarity, the unlike ones too: which of
    them count as a question asked again is the caller's threshold to apply, and
    the whole spread is what that threshold is chosen from.

    Left out: a pair with small talk on either side ("thanks" and "thanks a lot" are
    alike and mean nothing), and a pair asked more than ``window`` apart.

    ``vectors`` is keyed by the question's text, so two visitors asking the same
    words share one embedding.

    Two standard-library tools do the walking. ``itertools.groupby`` splits a sorted
    sequence into runs with the same key, here one run per conversation; it only
    groups neighbours, which is why the sort comes first. ``itertools.pairwise``
    turns ``a, b, c`` into ``(a, b), (b, c)``.
    """
    ordered = sorted(turns, key=lambda turn: (turn.conversation, turn.asked_at, turn.question_id))
    pairs: list[Pair] = []

    for _, conversation in itertools.groupby(ordered, key=lambda turn: turn.conversation):
        for first, second in itertools.pairwise(conversation):
            if "small_talk" in {first.route, second.route}:
                continue
            if second.asked_at - first.asked_at > window:
                continue
            similarity = cosine(vectors[first.question], vectors[second.question])
            pairs.append(Pair(first=first, second=second, similarity=similarity))

    return pairs


@dataclass(frozen=True)
class Spread:
    """The middle and the slow end of a set of timings."""

    count: int
    median: float
    # Nine in ten were at or under this.
    p90: float


def spread(values: Sequence[float]) -> Spread | None:
    """The median and 90th percentile, or None when there is nothing to measure.

    ``statistics.quantiles(values, n=10)`` returns the nine points that cut the data
    into ten equal parts, so the last of them is the 90th percentile.
    ``method="inclusive"`` treats the values as everything there is and not a sample
    of something larger, which keeps the answer inside the range of the data: the
    default would report a 90th percentile slower than the slowest answer.
    """
    if not values:
        return None
    if len(values) == 1:
        # quantiles() needs two points to draw a line between.
        return Spread(count=1, median=values[0], p90=values[0])
    return Spread(
        count=len(values),
        median=statistics.median(values),
        p90=statistics.quantiles(values, n=10, method="inclusive")[-1],
    )


def histogram(values: Iterable[float], *, width: float) -> list[tuple[float, int]]:
    """How many values fall in each band ``width`` wide: (lower edge, count), lowest first.

    Bands with nothing in them are left out. The ``round`` is for a value sitting on
    an edge: 0.3 / 0.05 is 5.999999999999999 in floating point, and without it 0.30
    would be counted in the band below its own.
    """
    counts: dict[int, int] = {}
    for value in values:
        band = math.floor(round(value / width, 9))
        counts[band] = counts.get(band, 0) + 1
    return [(band * width, count) for band, count in sorted(counts.items())]
