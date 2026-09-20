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
"""

from portfolio_ai.exceptions import ConfigError, PortfolioAIError, PurgeSafetyError

__version__ = "0.1.0"

__all__ = [
    "ConfigError",
    "PortfolioAIError",
    "PurgeSafetyError",
    "__version__",
]
