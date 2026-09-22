"""Parsing an article's frontmatter, and refusing to parse a broken one.

Pure functions over strings, so these need nothing: no database, no network, no
settings. That is the payoff for keeping parsing separate from the pipeline.
"""

import datetime as dt

import pytest

from portfolio_ai.exceptions import DocumentRejectedError
from portfolio_ai.ingestion.frontmatter import DocumentMeta, parse_document, split_frontmatter

VALID = """---
doc_id: about-mihail
title: About Mihail Mihaylov
page_type: about
url: https://mihaylov.io/
source_type: knowledgebase
tags: [profile, biography, experience]
last_verified: 2026-08-13
---

## Summary

Some prose.
"""


def test_parses_a_real_article() -> None:
    meta, body = parse_document("knowledgebase/about-mihail.md", VALID)

    assert meta.doc_id == "about-mihail"
    assert meta.title == "About Mihail Mihaylov"
    assert meta.tags == ["profile", "biography", "experience"]
    # YAML gives a real date, not a string. That matters because the column is a
    # date -- a string would be coerced by the driver or rejected, depending on
    # its format, and neither failure would name the cause.
    assert meta.last_verified == dt.date(2026, 8, 13)
    assert body.strip().startswith("## Summary")


def test_source_file_is_derived_from_the_url() -> None:
    meta, _ = parse_document("x.md", VALID)
    assert meta.source_file == "mihaylov.io.md"


def test_source_file_is_none_without_a_url() -> None:
    meta = DocumentMeta(doc_id="x", title="X")
    assert meta.source_file is None


def test_unknown_keys_are_ignored_not_rejected() -> None:
    """Unlike Settings, which forbids them. See the model's docstring."""
    text = VALID.replace("page_type: about", "page_type: about\ndraft: true")
    meta, _ = parse_document("x.md", text)
    assert meta.doc_id == "about-mihail"


@pytest.mark.parametrize(
    ("text", "because"),
    [
        ("## No frontmatter here\n\nbody", "no frontmatter block"),
        ("---\n---\n\nbody", "empty frontmatter block"),
        ("---\ntitle: X\n---\n\nbody", "missing doc_id"),
        ("---\ndoc_id: x\n---\n\nbody", "missing title"),
        ("---\ndoc_id: x\ntitle: ''\n---\n\nbody", "empty title"),
        ("---\n- a\n- b\n---\n\nbody", "frontmatter is a list, not a mapping"),
        ("---\ndoc_id: [unclosed\n---\n\nbody", "invalid YAML"),
    ],
)
def test_rejects_unusable_frontmatter(text: str, because: str) -> None:
    with pytest.raises(DocumentRejectedError) as caught:
        parse_document("knowledgebase/broken.md", text)

    # The path travels with the error because the run summary has to name the file
    # -- "a document was rejected" is not actionable.
    assert caught.value.path == "knowledgebase/broken.md", because


def test_tags_accept_a_comma_separated_string() -> None:
    """`tags: a, b` is natural to write and YAML reads it as one string."""
    text = VALID.replace("tags: [profile, biography, experience]", "tags: profile, biography")
    meta, _ = parse_document("x.md", text)
    assert meta.tags == ["profile", "biography"]


def test_missing_tags_become_an_empty_list() -> None:
    text = VALID.replace("tags: [profile, biography, experience]\n", "")
    meta, _ = parse_document("x.md", text)
    assert meta.tags == []


def test_a_later_horizontal_rule_is_body_not_frontmatter() -> None:
    """The pattern is anchored at the start, so `---` in prose stays in the body."""
    text = VALID + "\n---\n\nMore prose after a horizontal rule.\n"
    _, body = parse_document("x.md", text)
    assert "More prose after a horizontal rule." in body


def test_split_frontmatter_returns_the_whole_file_when_there_is_none() -> None:
    block, body = split_frontmatter("# Just markdown\n")
    assert not block
    assert body == "# Just markdown\n"
