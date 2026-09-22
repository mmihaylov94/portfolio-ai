"""Check knowledge base articles before they are committed.

    portfolio-ai-validate knowledgebase/

Run from the repository that *holds* the articles, in its own CI, so that a
malformed article fails in review rather than at four in the morning in a backend
log nobody is reading.

The important property is that this is not a second implementation of the rules.
It calls :func:`parse_document` and :func:`chunk_document` -- the exact functions
ingestion runs -- so "passes the checker" and "will be indexed" cannot drift
apart. A validator that is more lenient than the real parser gives false
confidence; one that is stricter blocks articles that would have been fine. The
only way to avoid both is to run the same code.

It needs no database, no network and no configuration: nothing here reads
``Settings``, which is what makes it runnable in another repository's CI with no
secrets at all.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from portfolio_ai.exceptions import DocumentRejectedError
from portfolio_ai.ingestion.chunking import MAX_TOKENS_PER_CHUNK, chunk_document
from portfolio_ai.ingestion.frontmatter import DocumentMeta, parse_document
from portfolio_ai.llm.pricing import estimate_tokens

app = typer.Typer(
    add_completion=False,
    help="Validate knowledge base articles against the rules ingestion applies.",
)

# Never appear in a URL the assistant is allowed to emit. These paths exist in the
# repository but not on the site, so a link containing one 404s for the visitor.
# The assistant is told this in its prompt and filtered on it in code; an article
# whose frontmatter carries such a URL would feed it one anyway.
FORBIDDEN_URL_FRAGMENTS = ("/knowledgebase/", "/projects/")


@dataclass
class Finding:
    """One problem with one file. ``fatal`` decides the exit code."""

    path: str
    message: str
    fatal: bool


def _check_one(path: Path, root: Path, seen_doc_ids: dict[str, str]) -> list[Finding]:
    """Everything checkable about a single article."""
    relative = path.relative_to(root).as_posix() if root in path.parents else path.name
    findings: list[Finding] = []

    try:
        meta, body = parse_document(relative, path.read_text(encoding="utf-8"))
    except DocumentRejectedError as exc:
        # The same rejection ingestion would produce, with the same message.
        return [Finding(relative, exc.reason, fatal=True)]

    # doc_id is the identity everything upserts against, so two articles sharing
    # one means the second silently replaces the first in the index -- both
    # embedded, one stored, no error anywhere. This is the check that exists
    # because nothing else can see it: each file is individually valid.
    if meta.doc_id in seen_doc_ids:
        findings.append(
            Finding(
                relative,
                f"doc_id {meta.doc_id!r} already used by {seen_doc_ids[meta.doc_id]}",
                fatal=True,
            )
        )
    else:
        seen_doc_ids[meta.doc_id] = relative

    findings.extend(_check_url(relative, meta))
    findings.extend(_check_dates(relative, meta))
    findings.extend(_check_structure(relative, body))

    return findings


def _check_url(relative: str, meta: DocumentMeta) -> list[Finding]:
    if meta.url is None:
        # Not fatal: the schema allows it. But the assistant cites articles by
        # URL, so one without a link is one it can mention and never point to.
        return [
            Finding(relative, "no url -- the assistant cannot link to this article", fatal=False)
        ]

    return [
        Finding(relative, f"url contains {fragment!r}, which is not a real page", fatal=True)
        for fragment in FORBIDDEN_URL_FRAGMENTS
        if fragment in meta.url
    ]


def _check_dates(relative: str, meta: DocumentMeta) -> list[Finding]:
    if meta.last_verified is None:
        return [Finding(relative, "no last_verified date", fatal=False)]

    # A day of slack, which removes a timezone argument rather than settling it.
    # "Today" differs by a calendar day between UTC and, say, New Zealand, and an
    # article dated correctly by its author should not fail in a CI runner set to
    # UTC. The mistake this catches is a typo in the year or the month, which is
    # never one day out.
    tomorrow = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)

    if meta.last_verified > tomorrow:
        # Usually a typo in the year, and it quietly defeats any later attempt to
        # find articles that have not been reviewed in a while.
        return [
            Finding(relative, f"last_verified is in the future ({meta.last_verified})", fatal=True)
        ]

    return []


def _check_structure(relative: str, body: str) -> list[Finding]:
    """The chunking rules, run for real rather than described."""
    chunks = chunk_document(body)

    if not chunks:
        return [Finding(relative, "no content -- nothing would be indexed", fatal=True)]

    findings: list[Finding] = []

    # One chunk called "main" means no H2 headings at all. It indexes, but the
    # whole article becomes a single embedding, which retrieves far less precisely
    # than one chunk per question. Worth saying; not worth blocking.
    if len(chunks) == 1 and chunks[0].section == "main":
        findings.append(
            Finding(
                relative, "no '## ' headings -- the whole article becomes one chunk", fatal=False
            )
        )

    findings.extend(
        Finding(
            relative,
            f"section {chunk.section_title!r} is ~{estimate_tokens(chunk.content)} tokens "
            f"and will be split (limit {MAX_TOKENS_PER_CHUNK})",
            fatal=False,
        )
        for chunk in chunks
        if estimate_tokens(chunk.content) > MAX_TOKENS_PER_CHUNK
    )

    return findings


@app.command()
def main(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Markdown files, or directories to search recursively."),
    ],
    strict: Annotated[
        bool,
        typer.Option("--strict", help="Treat warnings as failures too."),
    ] = False,
) -> None:
    """Validate every article, then exit non-zero if any of them is unusable."""
    files: list[Path] = []
    root = Path.cwd()

    for given in paths:
        if given.is_dir():
            files.extend(sorted(given.rglob("*.md")))
        elif given.suffix == ".md":
            files.append(given)
        else:
            typer.secho(f"skipping {given} (not a .md file)", fg=typer.colors.YELLOW)

    if not files:
        typer.secho("No Markdown files found.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    # Shared across files on purpose: uniqueness is a property of the corpus, not
    # of any one article, and it is the only check here that cannot be made by
    # looking at a single file.
    seen_doc_ids: dict[str, str] = {}
    findings: list[Finding] = []

    for file in files:
        findings.extend(_check_one(file, root, seen_doc_ids))

    errors = [f for f in findings if f.fatal]
    warnings = [f for f in findings if not f.fatal]

    for finding in errors:
        typer.secho(f"  error    {finding.path}: {finding.message}", fg=typer.colors.RED)
    for finding in warnings:
        typer.secho(f"  warning  {finding.path}: {finding.message}", fg=typer.colors.YELLOW)

    typer.echo(
        f"\n{len(files)} article(s) checked, {len(seen_doc_ids)} distinct doc_id(s), "
        f"{len(errors)} error(s), {len(warnings)} warning(s)."
    )

    if errors or (strict and warnings):
        raise typer.Exit(code=1)

    typer.secho("All good.", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
