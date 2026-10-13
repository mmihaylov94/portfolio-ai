"""Turns made by hand, for tests of the analytics that need no database.

``db/analytics.Turn`` is what the analytics read back from the chat tables. The
signals and the report are plain functions of a list of them, so their tests build
the list here instead of storing and reading rows.

Importable from both suites because pyproject.toml puts ``tests/`` on pytest's
``pythonpath``, as it is for ``openai_fake.py``.
"""

import datetime as dt
from decimal import Decimal

from portfolio_ai.db.analytics import Turn

START = dt.datetime(2026, 10, 12, 9, 0, tzinfo=dt.UTC)


def turn(  # ruff: ignore[too-many-arguments] -- every one is a column, and all are named
    question_id: int,
    question: str = "A question?",
    *,
    conversation: int = 1,
    minutes: float = 0,
    route: str = "mihail_related",
    top_score: float | None = 0.55,
    fallback_used: bool = False,
    answer: str = "An answer.",
    rating: int | None = None,
    comment: str | None = None,
    cost: str = "0.0006",
    latency_ms: int | None = 4000,
    first_token_ms: int | None = 3000,
) -> Turn:
    """One turn, asked ``minutes`` after :data:`START`. The answer's id is the
    question's plus one, as it is in the table."""
    searched = route == "mihail_related"
    return Turn(
        question_id=question_id,
        answer_id=question_id + 1,
        conversation=conversation,
        asked_at=START + dt.timedelta(minutes=minutes),
        question=question,
        answer=answer,
        route=route,
        top_score=top_score if searched else None,
        fallback_used=fallback_used,
        rating=rating,
        comment=comment,
        cost=Decimal(cost),
        latency_ms=latency_ms,
        first_token_ms=first_token_ms,
    )
