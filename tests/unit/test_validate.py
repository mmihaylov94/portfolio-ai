"""The article checker that runs in the portfolio repository's CI.

Driven through the CLI rather than the helper functions, because the exit code is
the whole contract: everything else is output for a human, and the thing another
repository's workflow acts on is whether this returned zero.
"""

import datetime as dt
from pathlib import Path

import pytest
from typer.testing import CliRunner

from portfolio_ai.ingestion.validate import app

runner = CliRunner()

GOOD = """---
doc_id: {doc_id}
title: {title}
page_type: about
url: https://mihaylov.io/{doc_id}
source_type: knowledgebase
tags: [one, two]
last_verified: 2026-01-01
---

## What is this?

An answer that is long enough to be worth retrieving.

## And another question?

A second answer.
"""


def write(directory: Path, name: str, content: str) -> None:
    (directory / name).write_text(content, encoding="utf-8")


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """A directory holding one valid article."""
    write(tmp_path, "good.md", GOOD.format(doc_id="good", title="Good"))
    return tmp_path


def test_a_valid_corpus_passes(corpus: Path) -> None:
    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert "All good." in result.output


def test_it_finds_articles_in_nested_directories(corpus: Path) -> None:
    """Real articles live under knowledgebase/projects/, so a non-recursive
    search would silently check half the corpus and report success."""
    nested = corpus / "projects"
    nested.mkdir()
    write(nested, "nested.md", GOOD.format(doc_id="nested", title="Nested"))

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert "2 article(s)" in result.output


def test_two_articles_sharing_a_doc_id_is_an_error(corpus: Path) -> None:
    """The check nothing else can make: each file is individually valid, and the
    consequence is one article silently replacing the other in the index."""
    write(corpus, "thief.md", GOOD.format(doc_id="good", title="Different Title"))

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 1
    assert "already used by" in result.output


def test_a_url_pointing_at_a_repository_path_is_an_error(corpus: Path) -> None:
    """`/knowledgebase/` and `/projects/` exist in git and not on the site, so a
    link containing one 404s for the visitor who follows it."""
    write(
        corpus,
        "bad.md",
        GOOD.format(doc_id="bad", title="Bad").replace(
            "https://mihaylov.io/bad", "https://mihaylov.io/knowledgebase/bad"
        ),
    )

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 1
    assert "not a real page" in result.output


def test_a_future_last_verified_is_an_error(corpus: Path) -> None:
    today = dt.datetime.now(dt.UTC).date()
    far_future = today.replace(year=today.year + 5)
    write(
        corpus,
        "future.md",
        GOOD.format(doc_id="future", title="Future").replace("2026-01-01", str(far_future)),
    )

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 1
    assert "in the future" in result.output


def test_a_date_one_day_ahead_is_tolerated(corpus: Path) -> None:
    """Deliberate slack, so an article dated correctly by its author does not fail
    in a runner whose clock is on the other side of midnight UTC."""
    tomorrow = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    write(
        corpus,
        "tomorrow.md",
        GOOD.format(doc_id="tomorrow", title="Tomorrow").replace("2026-01-01", str(tomorrow)),
    )

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0


def test_a_broken_article_reports_the_same_reason_ingestion_would(corpus: Path) -> None:
    """Not a paraphrase -- the message comes from parse_document itself, which is
    the point of calling the real parser rather than reimplementing it."""
    write(corpus, "broken.md", "## No frontmatter\n\nbody")

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 1
    assert "no frontmatter block" in result.output


def test_an_article_with_no_headings_warns_but_passes(corpus: Path) -> None:
    """It indexes; it just retrieves worse, as one embedding for the whole
    article. Worth saying, not worth blocking a commit over."""
    write(
        corpus,
        "flat.md",
        "---\ndoc_id: flat\ntitle: Flat\nurl: https://mihaylov.io/flat\n"
        "last_verified: 2026-01-01\n---\n\nJust prose.\n",
    )

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert "one chunk" in result.output


def test_strict_turns_warnings_into_failures(corpus: Path) -> None:
    write(
        corpus,
        "flat.md",
        "---\ndoc_id: flat\ntitle: Flat\nurl: https://mihaylov.io/flat\n"
        "last_verified: 2026-01-01\n---\n\nJust prose.\n",
    )

    assert runner.invoke(app, [str(corpus)]).exit_code == 0
    assert runner.invoke(app, [str(corpus), "--strict"]).exit_code == 1


def _project(title: str) -> str:
    article = GOOD.format(doc_id="project", title=title)
    return article.replace("page_type: about", "page_type: project")


@pytest.mark.parametrize(
    "title",
    [
        "Brand New Product",
        # Each is a substring of the prompt ("Threadline", "email") but not a word
        # of it, so a substring test would have let them through.
        "Thread",
        "Email Reporter",
    ],
)
def test_a_project_the_classifier_prompt_does_not_name_warns_but_passes(
    corpus: Path, title: str
) -> None:
    """golden_v1 found "What is Glotsmith?" refused as off-topic while the prompt
    did not name Glotsmith. A new project would go the same way unnoticed."""
    write(corpus, "new.md", _project(title))

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert "not named in the classifier prompt" in result.output


@pytest.mark.parametrize(
    "title",
    [
        "Glotsmith",
        "Threadline",
        "n8n Pro Automation Framework",
        "AI Marketing Reporter",
        # Worded differently in the prompt ("the AI assistant on mihaylov.io"), which
        # is why a title counts as named when all of its words appear, in any order.
        "mihaylov.io AI Assistant",
    ],
)
def test_every_real_project_is_named(corpus: Path, title: str) -> None:
    write(corpus, "project.md", _project(title))

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert "0 warning(s)" in result.output


def test_an_empty_directory_fails_rather_than_passing_vacuously(tmp_path: Path) -> None:
    """A checker that reports success when it found nothing to check is worse than
    no checker: a wrong path in CI would look green forever."""
    result = runner.invoke(app, [str(tmp_path)])

    assert result.exit_code == 1
    assert "No Markdown files found" in result.output


def test_it_needs_no_configuration(corpus: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that makes it usable in another repository's CI.

    Nothing here reads Settings, so there is no DATABASE_URL, no OPENAI_API_KEY
    and no .env involved. Proven by removing them from the environment entirely.
    """
    for name in ("DATABASE_URL", "OPENAI_API_KEY", "ENVIRONMENT", "DB_SCHEMA"):
        monkeypatch.delenv(name, raising=False)

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
