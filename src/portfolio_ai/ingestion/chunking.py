"""Splitting an article body into the pieces retrieval searches.

One chunk per H2 section. That sounds arbitrary and is not: the knowledge base is
deliberately authored as H2 questions --

    ## What is Mihail's job title?
    ## How much experience does Mihail have?

-- so a section is already a self-contained answer to a question somebody might
ask. Embedding it whole means the vector describes one complete idea, and the
retrieved text needs no assembly before it reaches the model. Keep that authoring
convention; it is why this works as well as it does on eleven documents.

The chunk text is ``"{section_title}\\n\\n{section_body}"``, heading included.
Including the heading matters more than it looks: the heading is usually the
question, and a visitor's question is much closer to it than to the prose
answering it. Dropping it measurably hurts retrieval.
"""

import re
from dataclasses import dataclass

from portfolio_ai.llm.pricing import estimate_tokens

# An H2 heading at the start of a line. Not H1 -- articles have no H1, the title
# lives in frontmatter -- and not H3, which subdivides within an answer and should
# stay attached to it.
_H2 = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Where an oversize section is cut. Paragraph break first, then sentence end, then
# whitespace: each is a worse place to break than the last, and cutting mid-word is
# the only option that is never acceptable.
_PARAGRAPH = re.compile(r"\n\s*\n")

# The embeddings model accepts 8191 tokens per input. This is the point at which a
# section is split instead, and the gap is deliberate: the estimate below is
# roughly four characters per token, which is wrong by a good margin on unusual
# text, and a split that happens slightly too early costs nothing while one that
# happens too late is a failed request mid-run.
#
# For scale, the largest section in the corpus today is about 590 tokens. Nothing
# approaches this, and that is the point of a guard.
MAX_TOKENS_PER_CHUNK = 6000


@dataclass(frozen=True)
class Chunk:
    """One embeddable piece of a document."""

    index: int
    section: str
    section_title: str
    content: str


def slugify(value: str) -> str:
    """Lower-case, punctuation-free, hyphen-separated.

    Ported deliberately from the n8n workflow, including the ``&`` to ``and`` rule,
    so that section identifiers stay the same across the cutover. They are not load
    bearing -- nothing joins on them -- but having them change for no reason would
    make the old and new tables impossible to compare while both exist.
    """
    text = value.lower().replace("&", " and ")
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s+", "-", text.strip())


def clean_text(value: str) -> str:
    """Normalise line endings and collapse runs of blank lines."""
    return re.sub(r"\n{3,}", "\n\n", value.replace("\r\n", "\n")).strip()


def _split_oversize(text: str) -> list[str]:
    """Break a too-large piece of text on the least-bad boundary available.

    Only reachable for a section several times larger than anything in the corpus.
    It exists because the alternative when one does appear is a failed API call in
    the middle of a scheduled run, reported as a 400 that says nothing about which
    document caused it.
    """
    if estimate_tokens(text) <= MAX_TOKENS_PER_CHUNK:
        return [text]

    paragraphs = _PARAGRAPH.split(text)
    parts: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph

        if estimate_tokens(candidate) <= MAX_TOKENS_PER_CHUNK:
            current = candidate
            continue

        if current:
            parts.append(current)

        # A single paragraph over the limit has no paragraph boundary to use, so
        # it is cut on character count. Ugly, and better than not sending it.
        if estimate_tokens(paragraph) > MAX_TOKENS_PER_CHUNK:
            width = MAX_TOKENS_PER_CHUNK * 4
            parts.extend(paragraph[i : i + width] for i in range(0, len(paragraph), width))
            current = ""
        else:
            current = paragraph

    if current:
        parts.append(current)

    return parts


def chunk_document(body: str) -> list[Chunk]:
    """Split an article body into chunks, in document order.

    Three cases, and the first is the one the n8n version gets wrong.

    **Content before the first heading** becomes an ``intro`` chunk. Today no
    article has any, so this changes nothing -- but writing an opening paragraph
    is a completely ordinary thing to do, and in the workflow being replaced that
    text is silently discarded. The article would simply answer worse, with no
    error anywhere and nothing to notice.

    **An article with no H2 headings at all** becomes one chunk called ``main``,
    which is what the n8n version does and is the right answer: the whole document
    is the smallest self-contained unit it has.

    **Everything else** is one chunk per section.
    """
    body = clean_text(body)

    if not body:
        return []

    headings = list(_H2.finditer(body))

    if not headings:
        return _build(("Main", "main", body))

    pieces: list[tuple[str, str, str]] = []

    preamble = clean_text(body[: headings[0].start()])
    if preamble:
        pieces.append(("Introduction", "intro", preamble))

    for position, heading in enumerate(headings):
        title = heading.group(1).strip()
        end = headings[position + 1].start() if position + 1 < len(headings) else len(body)
        content = clean_text(body[heading.end() : end])

        # A heading with nothing under it carries no information to retrieve, and
        # an embedding of a bare question with no answer is actively harmful: it
        # matches the question well and then contributes nothing to the answer.
        if not content:
            continue

        pieces.append((title, slugify(title), content))

    return _build(*pieces)


def _build(*pieces: tuple[str, str, str]) -> list[Chunk]:
    """Turn (title, section, content) triples into numbered chunks.

    Indices are assigned here, at the end, rather than while splitting -- an
    oversize section becomes several chunks, so the count is not known until the
    splitting is done. ``(document_id, chunk_index)`` is unique in the schema, so
    these have to be contiguous and gapless.
    """
    chunks: list[Chunk] = []

    for title, section, content in pieces:
        for part in _split_oversize(content):
            chunks.append(
                Chunk(
                    index=len(chunks),
                    section=section,
                    section_title=title,
                    # The heading is part of the embedded text. See the module
                    # docstring: it is usually the question being asked.
                    content=f"{title}\n\n{part}",
                )
            )

    return chunks
