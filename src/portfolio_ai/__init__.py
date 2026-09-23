"""Portfolio AI: RAG assistant, ingestion pipeline and eval harness.

Subpackages, in the order the system uses them:

- :mod:`portfolio_ai.ingestion` -- reads Markdown from GitHub, embeds it, keeps the
  vector store in step with the source repository
- :mod:`portfolio_ai.assistant` -- retrieval, intent classification, the agent loop
- :mod:`portfolio_ai.api` -- the FastAPI application the site talks to
- :mod:`portfolio_ai.analytics` -- what was asked, what failed, what to write next
- :mod:`portfolio_ai.evals` -- scoring the assistant so model changes are measurable

with :mod:`portfolio_ai.db` and :mod:`portfolio_ai.llm` underneath all of them.

Only the exception types are re-exported here. Everything else is imported from the
module that defines it, so that ``import portfolio_ai`` stays cheap and cannot
develop import cycles as the package grows.

This module does one thing on import, which lessons 3 and 4 said modules should
not do. See the comment below -- the exception is deliberate and the reasoning is
written down rather than assumed.
"""
# ruff: file-ignore[non-empty-init-module]  -- see the event loop comment below

import asyncio
import sys

from portfolio_ai.exceptions import (
    AssistantError,
    ConfigError,
    DocumentRejectedError,
    EmbeddingError,
    PortfolioAIError,
    PurgeSafetyError,
)

# Windows picks an event loop that psycopg's async mode cannot use. Without this,
# the first database call fails with:
#
#     InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
#
# which names an internal class and suggests nothing you would guess. It has to be
# set before any event loop starts, so the only place that reliably covers scripts,
# tests and one-liners alike is here, at import.
#
# One thing it cannot cover: a server that builds its own loop before importing any
# of this. uvicorn does exactly that, which is why the API's entry point names its
# loop outright (api/__main__.py). The development server, run with --reload, gets
# a compatible loop anyway: uvicorn runs the app in a subprocess then, and picks the
# selector loop for subprocesses.
#
# Doing work on import is exactly what config.py and logging.py avoid, and the
# trade is worth stating plainly: the alternative is two lines of boilerplate in
# every command that touches the database, and a baffling error whenever they are
# forgotten. On Linux this is a no-op, so production never runs it.
#
# ruff objects to an __init__.py containing anything but docstrings and re-exports,
# and the rule is right about why: importing a package should not have effects. The
# suppression at the top of this file is deliberate rather than a rewrite, because the
# reasoning above is the whole justification and a rule cannot read it.
if sys.platform == "win32":  # pragma: no cover - platform specific
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

__version__ = "0.1.0"

__all__ = [
    "AssistantError",
    "ConfigError",
    "DocumentRejectedError",
    "EmbeddingError",
    "PortfolioAIError",
    "PurgeSafetyError",
    "__version__",
]
