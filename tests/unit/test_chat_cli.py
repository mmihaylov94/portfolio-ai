"""The terminal chat command.

Run with ``--no-save`` and a fake model, the whole command works without a database
-- which is also how it is meant to be used against production. The settings it
reads come from ``conftest.py``, like every unit test's.
"""

import click
import pytest
from openai_fake import FakeOpenAI, install_fake_openai
from typer.testing import CliRunner, Result

from portfolio_ai.assistant.cli import app
from portfolio_ai.config import get_settings
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.documents import RetrievedChunk

CHUNK = RetrievedChunk(
    chunk_id=1,
    doc_id="tech-stack",
    title="Tech Stack",
    url="https://mihaylov.io/#about",
    section="php-laravel",
    section_title="Does Mihail work with Laravel?",
    content="Yes, it is his main PHP framework.",
    score=0.62,
)


def _text(result: Result) -> str:
    """What the command printed, without colour codes.

    Colour depends on where the tests run. typer forces it on whenever the
    GITHUB_ACTIONS variable is set, and then an error box styles `--no-save` in
    pieces, so the plain string never appears in the raw output. Locally the same
    box is printed plain. Assertions go through this so they hold in both places.
    """
    return click.unstyle(result.output)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    async def search_chunks(  # ruff: ignore[unused-async]
        vector: list[float],  # ruff: ignore[unused-function-argument]
        *,
        top_k: int,  # ruff: ignore[unused-function-argument]
    ) -> list[RetrievedChunk]:
        return [CHUNK]

    monkeypatch.setattr(docs_db, "search_chunks", search_chunks)
    return install_fake_openai(monkeypatch)


def test_one_question_prints_the_answer_and_what_it_cost(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")

    result = CliRunner().invoke(app, ["--no-save", "-m", "Does Mihail work with Laravel?"])

    assert result.exit_code == 0, result.output
    assert "Yes, Laravel is his main PHP framework." in _text(result)
    assert "mihail_related" in _text(result)
    assert 'searched "Laravel PHP experience" top 0.62' in _text(result)
    assert "4 calls" in _text(result)


def test_a_single_call_is_not_reported_as_one_calls(fake: FakeOpenAI) -> None:
    fake.classify("out_of_scope")

    result = CliRunner().invoke(app, ["--no-save", "-m", "What is the weather?"])

    assert "1 call " in _text(result) or _text(result).rstrip().endswith("1 call")


def test_verbose_shows_what_the_search_found(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes.")

    result = CliRunner().invoke(app, ["--no-save", "-v", "-m", "Laravel?"])

    assert "0.620  Tech Stack / Does Mihail work with Laravel?" in _text(result)


def test_a_failed_call_exits_non_zero(fake: FakeOpenAI) -> None:
    fake.http_error(500)

    result = CliRunner().invoke(app, ["--no-save", "-m", "Laravel?"])

    assert result.exit_code == 1
    assert "OpenAI call failed" in _text(result)


def test_it_refuses_to_write_into_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """A smoke test typed against production would be indistinguishable from a
    visitor's question in the analytics afterwards."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db:5432/portfolio_ai")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["-m", "Does Mihail work with Laravel?"])

    assert result.exit_code == 2
    assert "--no-save" in _text(result)


def test_production_is_allowed_when_nothing_is_saved(
    fake: FakeOpenAI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db:5432/portfolio_ai")
    get_settings.cache_clear()
    fake.classify("small_talk")
    fake.say("Hi there.")

    result = CliRunner().invoke(app, ["--no-save", "-m", "Hi"])

    assert result.exit_code == 0
    assert "Hi there." in _text(result)
