"""Splitting a Markdown file into its metadata and its body.

Knowledge base articles open with a YAML block between two ``---`` lines::

    ---
    doc_id: about-mihail
    title: About Mihail Mihaylov
    page_type: about
    url: https://mihaylov.io/
    source_type: knowledgebase
    tags: [profile, biography, experience]
    last_verified: 2026-08-13
    ---

    ## Summary
    ...

The n8n workflow this replaces parses that block with a hand-written line splitter
-- split on the first colon, strip quotes, special-case ``[a, b]`` into a list. It
works on the current files, and it is a YAML parser that handles about four per
cent of YAML. Every one of the eleven articles parses correctly with
``yaml.safe_load``, which also returns ``tags`` as a real list and ``last_verified``
as a real ``date`` rather than strings that something downstream has to convert.

``safe_load`` rather than ``load``: the unsafe loader can construct arbitrary
Python objects from a document, and this content arrives over the network from a
repository. It would take a repository compromise to matter, and that is exactly
the situation where you want the second lock to have been there all along.
"""

import datetime as dt
import re
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from portfolio_ai.exceptions import DocumentRejectedError

# The opening block: --- on its own line, anything, --- on its own line.
# Anchored at the start, so a --- later in the document (a horizontal rule) is
# body content and not a second frontmatter block.
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


class DocumentMeta(BaseModel):
    """Validated frontmatter for one article.

    A Pydantic model rather than a dataclass because this is external input
    crossing a boundary -- the line CLAUDE.md draws -- and because the validation
    *is* the feature. A missing ``doc_id`` has to become a rejection with a
    readable reason, which is what a model gives for free and a dataclass does not.

    ``extra="ignore"`` rather than ``forbid``, unlike Settings. The opposite
    reasoning applies: an unknown environment variable is almost always a typo in
    a name we chose, while an unknown frontmatter key is usually somebody adding
    metadata for another purpose. Refusing to index an article because it grew a
    ``draft:`` key would be obstructive.
    """

    model_config = {"extra": "ignore"}

    # The stable identity. Everything upserts and purges against this, so it must
    # be present and must not be derived from anything editable -- a doc_id
    # generated from the title changes the moment somebody rewrites the heading,
    # and the old document is then orphaned rather than updated.
    doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)

    source_type: str = "knowledgebase"
    page_type: str | None = None
    url: str | None = None
    tags: list[str] = Field(default_factory=list)
    last_verified: dt.date | None = None

    @field_validator("doc_id", "title", "source_type", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("tags", mode="before")
    @classmethod
    def _normalise_tags(cls, value: object) -> object:
        """Accept a YAML list, or a comma-separated string, or nothing.

        The articles all use list syntax today. The string branch exists because
        ``tags: automation, ai`` is a completely natural thing to write and YAML
        reads it as one string, which would otherwise become a single tag
        containing a comma -- wrong in a way nobody would notice.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def source_file(self) -> str | None:
        """The last path segment of ``url``, plus ``.md``.

        Carried over from the n8n workflow, which derives it the same way. It is
        descriptive metadata rather than anything the system depends on -- the
        authoritative location is ``source_path``, which comes from the repository
        tree and cannot disagree with reality.
        """
        if not self.url:
            return None
        segments = [part for part in self.url.split("/") if part]
        return f"{segments[-1]}.md" if segments else None


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_block, body)``. Both may be empty.

    A file with no frontmatter is not an error at this level -- it is a file with
    no metadata, and whether that is acceptable is decided by :func:`parse_document`.
    """
    match = _FRONTMATTER.match(text)

    if match is None:
        return "", text

    return match.group(1), text[match.end() :]


def parse_document(path: str, text: str) -> tuple[DocumentMeta, str]:
    """Parse one article into metadata and body, or reject it.

    Every failure here raises :class:`DocumentRejectedError` carrying the path and
    a readable reason, because that is what the run summary needs to print. The
    pipeline catches it per document and keeps going.
    """
    block, body = split_frontmatter(text)

    if not block.strip():
        raise DocumentRejectedError(path, "no frontmatter block")

    try:
        loaded: Any = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        # The message from PyYAML names the line and column, which is worth
        # keeping -- but it is multi-line, and this ends up in a log field and a
        # summary line, so it is flattened.
        reason = " ".join(str(exc).split())
        raise DocumentRejectedError(path, f"frontmatter is not valid YAML: {reason}") from exc

    if not isinstance(loaded, dict):
        raise DocumentRejectedError(
            path, f"frontmatter is {type(loaded).__name__}, expected a mapping of keys to values"
        )

    try:
        meta = DocumentMeta.model_validate(loaded)
    except ValidationError as exc:
        missing = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        raise DocumentRejectedError(path, f"invalid frontmatter: {missing}") from exc

    return meta, body
