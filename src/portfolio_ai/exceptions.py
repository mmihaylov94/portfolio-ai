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


class PurgeSafetyError(PortfolioAIError):
    """Ingestion found suspiciously few documents, so the stale-row purge was refused.

    The ingestion pipeline deletes any document it did not see in the current run.
    That is how files deleted from the source repository leave the vector store --
    and it is also exactly how a partial failure upstream would wipe the knowledge
    base. If GitHub returns two documents instead of eleven, the purge would treat
    the other nine as deleted.

    So discovery is checked against a floor before anything is removed. The n8n
    implementation this project replaces had no such guard.
    """

    def __init__(self, found: int, minimum: int) -> None:
        self.found = found
        self.minimum = minimum
        super().__init__(
            f"Discovery returned {found} documents, below the safety floor of {minimum}. "
            f"Refusing to purge: this would delete most of the knowledge base."
        )
