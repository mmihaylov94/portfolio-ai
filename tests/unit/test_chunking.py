"""Splitting an article body into chunks.

These are the decisions retrieval quality rests on, so they are tested as
behaviour rather than as implementation: what comes out, not how it got there.
"""

import pytest

from portfolio_ai.ingestion.chunking import (
    MAX_TOKENS_PER_CHUNK,
    chunk_document,
    clean_text,
    slugify,
)

ARTICLE = """
## What is Mihail's job title?

Solutions Architect.

## How much experience does Mihail have?

Since May 2020.
"""


def test_one_chunk_per_h2_section() -> None:
    chunks = chunk_document(ARTICLE)

    assert len(chunks) == 2
    assert [c.section for c in chunks] == [
        "what-is-mihails-job-title",
        "how-much-experience-does-mihail-have",
    ]
    assert [c.index for c in chunks] == [0, 1]


def test_the_heading_is_part_of_the_embedded_text() -> None:
    """Not decoration. The heading is the question; a visitor's question is far
    closer to it than to the prose answering it."""
    chunks = chunk_document(ARTICLE)

    assert chunks[0].content == "What is Mihail's job title?\n\nSolutions Architect."


def test_an_article_with_no_headings_becomes_one_main_chunk() -> None:
    chunks = chunk_document("Just a paragraph, no structure at all.")

    assert len(chunks) == 1
    assert chunks[0].section == "main"
    assert chunks[0].section_title == "Main"


def test_content_before_the_first_heading_becomes_an_intro_chunk() -> None:
    """The bug in the workflow being replaced: this text is silently dropped there.

    Nothing in the corpus has a preamble today, so this protects a behaviour that
    is currently unexercised -- which is exactly when a regression goes unnoticed.
    """
    chunks = chunk_document("An opening paragraph.\n" + ARTICLE)

    assert chunks[0].section == "intro"
    assert "An opening paragraph." in chunks[0].content
    assert len(chunks) == 3


def test_a_heading_with_no_content_is_skipped() -> None:
    """An embedding of a bare question with no answer is worse than nothing: it
    matches the question well and contributes nothing to the reply."""
    chunks = chunk_document("## Empty section\n\n## Real section\n\nWith content.")

    assert len(chunks) == 1
    assert chunks[0].section == "real-section"


def test_indices_stay_contiguous_when_a_section_is_skipped() -> None:
    """(document_id, chunk_index) is unique in the schema, so gaps are not free."""
    chunks = chunk_document("## A\n\nOne.\n\n## Empty\n\n## B\n\nTwo.")

    assert [c.index for c in chunks] == [0, 1]


def test_h3_stays_inside_its_section() -> None:
    """H3 subdivides an answer; splitting there would separate a detail from the
    question it answers."""
    chunks = chunk_document("## Question\n\nAnswer.\n\n### Detail\n\nMore.")

    assert len(chunks) == 1
    assert "### Detail" in chunks[0].content


def test_an_empty_body_produces_nothing() -> None:
    assert chunk_document("   \n\n  ") == []


def test_an_oversize_section_is_split_on_paragraphs() -> None:
    paragraph = "word " * 2000  # ~10_000 characters, ~2_500 estimated tokens
    body = f"## Huge\n\n{paragraph}\n\n{paragraph}\n\n{paragraph}"

    chunks = chunk_document(body)

    assert len(chunks) > 1
    # Every part still carries the heading, so a retrieved fragment is still
    # attributable to the question it answers.
    assert all(c.content.startswith("Huge\n\n") for c in chunks)
    assert all(c.section == "huge" for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_a_single_oversize_paragraph_is_cut_on_characters() -> None:
    """Ugly, and better than a failed API call mid-run."""
    body = "## Huge\n\n" + ("x" * (MAX_TOKENS_PER_CHUNK * 4 * 3))

    chunks = chunk_document(body)

    assert len(chunks) > 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("What is Mihail's job title?", "what-is-mihails-job-title"),
        ("Tech & tools", "tech-and-tools"),
        ("  Leading and trailing  ", "leading-and-trailing"),
        ("Already-hyphenated", "already-hyphenated"),
        ("Multiple   spaces", "multiple-spaces"),
    ],
)
def test_slugify(value: str, expected: str) -> None:
    """Ported from the n8n workflow, `&` rule included, so section identifiers
    stay comparable across the cutover while both systems exist."""
    assert slugify(value) == expected


def test_clean_text_normalises_line_endings_and_blank_runs() -> None:
    assert clean_text("a\r\n\n\n\n\nb\n\n") == "a\n\nb"
