"""Logging setup: one JSON object per line, on stdout, from every source.

Log lines here are not sentences. A call looks like this::

    log.info("documents_indexed", count=11, duration_ms=840)

and comes out as a JSON object with ``event``, ``count`` and ``duration_ms`` as
separate fields. That shape is the point: deliverable 4 in ARCHITECTURE.md counts
and groups these, and you cannot group a sentence.

Everything goes to stdout, in one shape, including output from libraries that have
never heard of structlog -- see :func:`configure_logging` for how.

Note for anyone startled by the filename: this module is called ``logging`` and sits
inside a package, which is fine. ``import logging`` below gets the standard library,
because Python only looks at what is directly inside the folders on its search path,
and this file is one level deeper than that. Lesson 2 covers why.
"""

import logging
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from portfolio_ai.config import get_settings


def _redact_secrets(
    logger: WrappedLogger,  # ruff: ignore[unused-function-argument]
    method_name: str,  # ruff: ignore[unused-function-argument]
    event_dict: EventDict,
) -> EventDict:
    """Replace the value of any field whose name suggests it holds a secret.

    The first two arguments are never used and cannot be removed: this signature is
    structlog's, not ours, and every processor in the chain is called with all three.
    That is a category ruff's unused-argument rule is structurally bad at, because it
    cannot tell a parameter nobody needed from one a caller insists on passing. Naming
    them ``_logger`` and ``_method_name`` would silence it and would also hide which
    protocol this function is implementing, which is the more useful information.

    ``SecretStr`` in config.py already stops the OpenAI key printing when the
    settings object is logged. This is the second layer, for dictionaries that
    arrive from somewhere else -- an API response, a database row, a set of
    request headers -- where nobody chose the type. It applies to third-party
    libraries too, since their output goes through this same chain.

    The matching is deliberately conservative about the word "key". A bare
    ``key`` is left alone: it is an ordinary English word, and matching it would
    also catch ``monkey`` and ``keyboard_layout``, which is how redaction ends
    up destroying the logs it was meant to protect. Requiring ``_key`` keeps
    ``api_key``, ``private_key`` and ``signing_key`` while leaving those alone.
    Anything genuinely sensitive is almost always qualified.

    ``token`` needed the same treatment, and did not get it until it bit. It was
    matched as a plain substring, which also catches ``total_tokens`` and
    ``prompt_tokens`` -- so the first ingestion run reported ``"total_tokens":
    "***"`` and every OpenAI call in the project logged its usage as three
    asterisks. Nothing failed. The cost reporting simply returned nothing, which
    is precisely the failure this docstring already warned about and did not
    prevent: over-matching quietly replaces data you needed, and it is much harder
    to notice than a leak.

    So a secret ``token`` is the whole field name or the end of it, and a count is
    not. ``github_token`` and ``access_token`` are redacted; ``total_tokens`` and
    ``token_count`` are left alone.

    Add terms as new ones turn up, and think about the plural each time.
    """
    for key in list(event_dict):
        lowered = key.lower()

        # No legitimate field name in this project contains any of these.
        substring_match = any(
            word in lowered for word in ("_key", "apikey", "password", "secret", "authorization")
        )
        # "token" is a credential; "tokens" is a quantity of them.
        token_match = lowered == "token" or lowered.endswith("_token")

        if substring_match or token_match:
            event_dict[key] = "***"

    return event_dict


def _processor_chain() -> list[Processor]:
    """The line of functions every log entry passes along, in order.

    Each one receives the event dictionary and returns it, having added to it or
    changed it. Order matters: the timestamp has to be added before anything can
    render it, and redaction has to happen before the entry is turned into text.
    """
    return [
        # Pull in anything bound with bind_contextvars(). Goes first so the rest
        # of the chain sees those fields too.
        structlog.contextvars.merge_contextvars,
        # Which logger produced this -- "portfolio_ai.ingestion", "psycopg.pool".
        structlog.stdlib.add_logger_name,
        # level: "info", "warning", ...
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # Turn an exception being handled into a "exception" field holding the
        # traceback, so a failure arrives as one log entry rather than a stack
        # trace interleaved with everything else.
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redact_secrets,
    ]


def configure_logging(level: str | None = None) -> None:
    """Point every log in the process at stdout, rendered as JSON.

    Called explicitly by entry points rather than running when this module is
    imported. Importing a package should not quietly reconfigure the whole
    process's logging; that belongs to whoever owns the program.

    ``level`` defaults to ``settings.log_level``. Note the ordering that implies:
    settings must be valid before logging is configured. That is deliberate --
    if the configuration is broken the process is not going to start anyway, and
    the resulting ConfigError prints perfectly well without any of this.
    """
    if level is None:
        level = get_settings().log_level

    shared = _processor_chain()

    # Our own loggers. wrap_for_formatter hands the finished dictionary to the
    # stdlib formatter below rather than rendering it here, so both paths end up
    # rendered by the same code.
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Python's own logging system, which is what psycopg, httpx and uvicorn use.
    #
    # Three pieces, and it is worth knowing which is which: a *logger* is the
    # named thing code calls; a *handler* decides where output goes; a *formatter*
    # decides what it looks like. We attach one handler to the root logger, and
    # give it a formatter that runs the structlog chain -- so a library line takes
    # the same route ours does and comes out in the same shape.
    formatter = structlog.stdlib.ProcessorFormatter(
        # Applied to entries that came from stdlib rather than structlog, to give
        # them the same fields ours get.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace rather than append, so calling this twice does not produce every
    # line twice.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
