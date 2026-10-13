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


def _saying(sentence: str) -> str:
    """A valid article whose second answer is ``sentence``."""
    return GOOD.format(doc_id="models", title="Models").replace("A second answer.", sentence)


@pytest.mark.parametrize(
    ("sentence", "named"),
    [
        # In the price table.
        ("OpenAI's gpt-5-mini writes the answers.", "gpt-5-mini"),
        ("The judge is gpt-6.1-sol.", "gpt-6.1-sol"),
        (
            "Embeddings come from text-embedding-3-small, at 1,536 dimensions.",
            "text-embedding-3-small",
        ),
        # Not in it: a model that does not exist yet, and a dated name.
        ("It moved to gpt-7-nova.", "gpt-7-nova"),
        ("Pinned to gpt-6-luna-2026-09-15.", "gpt-6-luna-2026-09-15"),
        # As a model writes a name when it is not copying one.
        ("It uses GPT-5 mini for answers.", "gpt-5"),
        # Letters written straight against the number.
        ("An earlier draft used gpt-4o-mini.", "gpt-4o-mini"),
        # With the non-breaking hyphens a model writes, which a name copied out of an
        # answer still has. \N{...} names the character, so that it shows in the source.
        (
            "It runs on gpt\N{NON-BREAKING HYPHEN}5\N{NON-BREAKING HYPHEN}mini.",
            "gpt-5-mini",
        ),
        # At the start of a line, with nothing before it but the line break.
        ("The models, one to a line:\ngpt-6-luna\nand an embedding model.", "gpt-6-luna"),
    ],
    ids=[
        "answers",
        "judge",
        "embeddings",
        "unpriced",
        "dated",
        "as-a-name",
        "letters-on-the-number",
        "non-breaking-hyphens",
        "start-of-a-line",
    ],
)
def test_an_article_that_names_a_model_warns_but_passes(
    corpus: Path, sentence: str, named: str
) -> None:
    """The assistant's own article named the model that writes the answers, an eval case
    required answers to name it too, and the first change of model made both false. The
    evals exist to compare models, so neither may pin one."""
    write(corpus, "models.md", _saying(sentence))

    result = runner.invoke(app, [str(corpus)])

    assert result.exit_code == 0
    assert f"names a model ({named})" in result.output
    assert "1 warning(s)" in result.output


def test_two_models_named_in_one_article_are_one_warning_naming_each_once(corpus: Path) -> None:
    """One named twice is listed once. And "gpt-5" is not listed at all: the sentence
    never names it, it only holds those characters at the start of a longer name."""
    write(corpus, "models.md", _saying("gpt-5-mini answers, gpt-6-luna judges, gpt-5-mini routes."))

    result = runner.invoke(app, [str(corpus)])

    assert "names a model (gpt-5-mini, gpt-6-luna)" in result.output
    assert "1 warning(s)" in result.output


@pytest.mark.parametrize(
    "sentence",
    [
        "OpenAI's language models write the answers, and an embedding model powers the search.",
        "It is not ChatGPT, and it is not chatgpt-5 either.",
        "The exact models are deliberately not named here.",
        "PostgreSQL with pgvector, queried through psycopg 3.",
        # A name from the price table inside a longer word is not that name.
        "Nothing here is called text-embedding-3-smaller or my-text-embedding-3-small.",
    ],
    ids=[
        "provider-only",
        "a-product-name",
        "says-so",
        "other-names-with-digits",
        "inside-a-longer-word",
    ],
)
def test_an_article_that_names_no_model_is_not_warned_about(corpus: Path, sentence: str) -> None:
    write(corpus, "models.md", _saying(sentence))

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
