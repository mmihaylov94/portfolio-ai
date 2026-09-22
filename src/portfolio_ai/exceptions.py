"""Errors raised deliberately by this package.

Everything here inherits from :class:`PortfolioAIError`, so a caller can catch the
whole surface of "something this project decided to reject" with one clause, and
still let genuine bugs propagate:

    try:
        run_ingestion()
    except PortfolioAIError:
        ...          # we said no, on purpose, for a reason we can report
    # TypeError, KeyError and friends are not caught here -- they are bugs.

That distinction is the point of a root exception class. Catching ``Exception``
would swallow programming mistakes along with intended refusals.

This module imports nothing. That is deliberate: it is imported by almost every
other module in the package, so any import it acquired would become a dependency
of the entire codebase and a likely source of circular imports.
"""


class PortfolioAIError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(PortfolioAIError):
    """Configuration is missing, malformed, or points somewhere it should not.

    Raised while settings are being loaded, which means at process startup rather
    than midway through serving a request. A misconfigured process should refuse
    to boot; it should not accept a request and fail halfway through it.
    """


class DocumentRejectedError(PortfolioAIError):
    """A source document could not be understood, so it was left out of the index.

    Raised while parsing frontmatter, and caught by the pipeline: one unusable file
    should not stop the other ten from being updated, least of all on a scheduled
    run at four in the morning with nobody watching.

    Rejecting is deliberate, and the alternative is worse than it looks. Defaulting
    a missing title to "Untitled" and a missing ``doc_id`` to a slug of that title
    -- which is what the n8n workflow does today -- produces a document that indexes
    cleanly, retrieves, answers vaguely, and whose identity changes the moment
    somebody edits the heading. A document that is absent is visible in the run
    summary. A document that is present and wrong is not visible anywhere.
    """

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


class EmbeddingError(PortfolioAIError):
    """The embeddings API returned something unusable.

    Not for network failures or rate limits -- the OpenAI SDK retries those itself,
    and if it gives up the exception it raises is more informative than anything
    this could add. This is for a response that arrived successfully and does not
    match what was asked for: the wrong number of vectors back, or vectors of the
    wrong width. Storing those would corrupt the index silently, because a vector
    column accepts any vector of the right dimension regardless of what it means.
    """


class PurgeSafetyError(PortfolioAIError):
    """A run would have deleted an implausible share of the knowledge base.

    The ingestion pipeline deletes any document it did not see in the current run.
    That is how files deleted from the source repository leave the vector store --
    and it is also exactly how a partial failure upstream would wipe it. If GitHub
    returns two documents instead of eleven, the other nine look deleted.

    So the size of the deletion is checked against a limit first. The limit is a
    *share* of what is stored rather than a fixed number of documents, because a
    fixed number stops protecting as the corpus grows: a floor of eight is a real
    guard over eleven documents and no guard at all over fifty, where a collapse to
    nine would clear forty-one and still pass. The danger was always proportional;
    the guard should be too.

    The n8n implementation this project replaces has no guard of any kind.
    """

    def __init__(self, *, stored: int, surviving: int, limit: int) -> None:
        self.stored = stored
        self.surviving = surviving
        self.deletions = max(0, stored - surviving)
        self.limit = limit
        super().__init__(
            f"This run would delete {self.deletions} of {stored} documents, "
            f"above the limit of {limit}. Refusing to purge: "
            f"only {surviving} document(s) survived discovery and parsing."
        )
