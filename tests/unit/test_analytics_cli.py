"""``python -m portfolio_ai.analytics purge``, as the crontab runs it.

The SQL is tested against a real database in tests/integration/test_api_db.py. This
is the command around it: that ``purge`` is a subcommand at all (a Typer app with
one command and no callback would not have one), that ``--dry-run`` only counts,
and that retention switched off says so instead of quietly doing nothing.
"""

import datetime as dt

import click
import pytest
from typer.testing import CliRunner

from portfolio_ai.analytics.cli import app
from portfolio_ai.config import get_settings
from portfolio_ai.db import chat as chat_db
from portfolio_ai.db.chat import Purged


@pytest.fixture
def swept(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dt.datetime]]:
    """Record which of the two queries ran, and with what cutoff."""
    calls: list[tuple[str, dt.datetime]] = []

    async def count_older_than(cutoff: dt.datetime) -> Purged:  # ruff: ignore[unused-async]
        calls.append(("count", cutoff))
        return Purged(messages=12, sessions=3)

    async def delete_older_than(cutoff: dt.datetime) -> Purged:  # ruff: ignore[unused-async]
        calls.append(("delete", cutoff))
        return Purged(messages=12, sessions=3)

    monkeypatch.setattr(chat_db, "count_older_than", count_older_than)
    monkeypatch.setattr(chat_db, "delete_older_than", delete_older_than)
    return calls


def test_purge_is_a_subcommand_and_deletes(swept: list[tuple[str, dt.datetime]]) -> None:
    result = CliRunner().invoke(app, ["purge"])

    assert result.exit_code == 0, result.output
    assert [kind for kind, _ in swept] == ["delete"]
    assert "PURGE COMPLETE" in click.unstyle(result.output)
    assert "messages       12" in click.unstyle(result.output)


def test_the_cutoff_is_the_retention_period_ago(swept: list[tuple[str, dt.datetime]]) -> None:
    before = dt.datetime.now(dt.UTC)

    CliRunner().invoke(app, ["purge"])

    [(_, cutoff)] = swept
    assert abs((before - dt.timedelta(days=90)) - cutoff) < dt.timedelta(minutes=1)


def test_a_dry_run_only_counts(swept: list[tuple[str, dt.datetime]]) -> None:
    result = CliRunner().invoke(app, ["purge", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert [kind for kind, _ in swept] == ["count"]
    assert "PLAN (nothing was deleted)" in click.unstyle(result.output)


def test_retention_switched_off_says_so(
    swept: list[tuple[str, dt.datetime]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["purge"])

    assert result.exit_code == 0
    assert swept == []
    assert "RETENTION IS OFF" in click.unstyle(result.output)
