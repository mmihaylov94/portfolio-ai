"""The retention sweep: chat older than ``CHAT_RETENTION_DAYS`` is deleted, nightly.

People type things into a chat box that they would not put in a form -- "I'm hiring
for a Laravel role at Acme" -- and this project keeps their words to learn from
them. Ninety days is long enough to build eval cases and see what people ask about
from one month to the next, and short enough to state plainly in a privacy policy.
This module is what keeps that sentence true.

What survives the sweep is what was learned rather than what was said: content
gaps, aggregates, and questions promoted into the eval dataset, which outlive the
conversation they came from by design (see ``db/chat.py``, ``delete_older_than``).
"""

import datetime as dt
from dataclasses import dataclass

import structlog

from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RetentionReport:
    """What one sweep did, or would have done."""

    days: int
    # None when retention is switched off and nothing was looked at.
    cutoff: dt.datetime | None
    messages: int
    sessions: int
    dry_run: bool


async def purge(*, dry_run: bool = False) -> RetentionReport:
    """Delete chat messages and sessions older than the retention period.

    ``dry_run`` counts instead of deleting, with the same cutoff, so what it prints
    is exactly what a real run would remove at that moment.
    """
    days = get_settings().chat_retention_days

    if days == 0:
        # Allowed, because a switch is useful while investigating something. Said
        # every night at warning level, because it is the privacy policy being
        # quietly not kept.
        log.warning("retention_disabled", reason="CHAT_RETENTION_DAYS=0")
        return RetentionReport(days=0, cutoff=None, messages=0, sessions=0, dry_run=dry_run)

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    found = (
        await chat_db.count_older_than(cutoff)
        if dry_run
        else await chat_db.delete_older_than(cutoff)
    )

    # Counts and the cutoff. Nothing about which conversations -- that would be a
    # record of the data this exists to remove.
    log.info(
        "retention_purged" if not dry_run else "retention_planned",
        days=days,
        cutoff=cutoff.isoformat(),
        messages=found.messages,
        sessions=found.sessions,
    )
    return RetentionReport(
        days=days,
        cutoff=cutoff,
        messages=found.messages,
        sessions=found.sessions,
        dry_run=dry_run,
    )
