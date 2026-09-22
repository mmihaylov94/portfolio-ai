"""Settings for the whole project, read from the environment exactly once.

Nothing else in this codebase reads ``os.environ``. Everything that needs a value
calls :func:`get_settings` and takes a typed field off the result, which means there
is one place where a name can be mistyped, one place where a value gets checked, and
one moment when a bad configuration can bring the process down: startup, before any
work has been accepted.

The alternative -- reaching for ``os.environ`` wherever a value happens to be needed
-- fails in a much worse shape. The name is re-typed at every call site, every value
arrives as a string that each caller parses in its own way, and a missing variable is
discovered halfway through serving a request rather than when the process boots.
"""

from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from portfolio_ai.exceptions import ConfigError

# Named rather than written inline in the check below. A bare 5433 in a comparison
# says nothing about which of the two local databases it is, and the same digits then
# have to be repeated in the error message.
_LOCAL_PGVECTOR_PORT = 5433

# How hard a reasoning model thinks before answering. Declared here rather than next
# to the code that sends it (llm/responses.py) because Settings needs it, and this
# module cannot import from llm/ -- llm/ imports this module, and the two would then
# each need the other to finish loading first.
type ReasoningEffort = Literal["minimal", "low", "medium", "high"]


class Settings(BaseSettings):
    """Every configurable value the project currently uses.

    Fields map to environment variables by name, upper-cased: ``db_schema`` is read
    from ``DB_SCHEMA``. Values come from the real environment first and ``.env``
    second, so an exported variable beats the file -- which is what you want in a
    container, where the environment is the deployment and the file usually is not
    there at all.

    Fields are added by the lesson that first needs them. Declaring everything in
    ``ARCHITECTURE.md`` up front would mean guessing at defaults months before the
    code that reads them exists.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Reject settings we do not recognise instead of ignoring them. A typo like
        # DATABSE_URL would otherwise be silently dropped, the field would fall back
        # to its default, and the resulting failure would surface a long way from the
        # cause. This checks the .env file and explicit arguments; the surrounding
        # OS environment is not policed, so PATH and friends are unaffected.
        extra="forbid",
    )

    # Which environment this process is running in. Literal rather than str, so a
    # value of "prod" or "Production" is rejected at startup rather than quietly
    # failing an equality check somewhere later.
    environment: Literal["local", "production"] = "local"

    # No default. A missing DATABASE_URL should stop the process, not connect it to
    # something plausible-looking.
    database_url: str
    db_schema: str = "portfolio_rag"
    # How many connections the pool may hold open at once. Postgres has a server-wide
    # limit shared with everything else on the instance, so this is a share of a
    # common resource rather than a private dial.
    db_pool_max_size: int = Field(default=10, ge=1, le=100)

    # SecretStr rather than str: see the lesson. Printing this gives "**********".
    openai_api_key: SecretStr

    # How long to wait on an OpenAI call, and how many times the SDK retries a
    # failure of its own accord. The SDK retries connection errors, timeouts, 429s
    # and 5xx with exponential backoff; anything else surfaces immediately.
    openai_timeout_seconds: float = Field(default=30.0, gt=0)
    openai_max_retries: int = Field(default=3, ge=0, le=10)

    embedding_model: str = "text-embedding-3-small"
    # Changing either of these invalidates every vector already stored, so they are
    # configuration in the sense of "recorded", not "tweakable".
    embedding_dimensions: int = Field(default=1536, ge=1)
    # Inputs per embeddings request. The corpus is ~111 chunks, so this is one or
    # two calls -- but a batch that grows without a ceiling eventually meets the
    # per-request token limit, and finding that boundary in production is worse
    # than never approaching it.
    embedding_batch_size: int = Field(default=64, ge=1, le=2048)

    retrieval_top_k: int = Field(default=20, ge=1, le=100)

    # --- Assistant ------------------------------------------------------------
    # The defaults are what the n8n workflow runs, so the first version of the
    # assistant is a port rather than a port plus changes nobody measured. Step 5's
    # evals are where any of these should move, one at a time.
    chat_model: str = "gpt-5-mini"
    classifier_model: str = "gpt-5-mini"
    # Unset sends nothing, so the model's own default applies -- medium, for
    # gpt-5-mini, which is also what n8n sent by never setting it. `minimal` halved
    # the classifier's latency in a nine-question check; that is a hint, not evidence.
    classifier_reasoning_effort: ReasoningEffort | None = None
    chat_reasoning_effort: ReasoningEffort | None = None
    # How much conversation the model sees, in exchanges: one question plus one
    # answer. 25 is n8n's number, and n8n's means exchanges too -- its memory node
    # hands LangChain k=25, which returns the last 2 * k messages. Earlier project
    # docs read it as 25 messages.
    memory_window_turns: int = Field(default=25, ge=0, le=100)
    # How many times the model may search in one answer before it must reply. The
    # first search is required; after this many the next call has tools switched
    # off. n8n allowed ten iterations -- a limit that only matters when something
    # has gone wrong, which is when a lower one is cheaper.
    agent_max_search_rounds: int = Field(default=3, ge=1, le=10)

    # --- Ingestion source -----------------------------------------------------
    # The repository ingestion reads from. Public, so the token below is optional.
    github_repo: str = "mmihaylov94/my-portfolio"
    github_branch: str = "main"
    # Only files under this folder are indexed. Widening it is a decision with
    # consequences -- see ARCHITECTURE.md section 7: the answer to "the assistant
    # could not answer that" is a new article, not a bigger crawl.
    github_docs_path: str = "knowledgebase"
    # Optional. Unauthenticated GitHub API calls are limited to 60 per hour per IP
    # and a run makes one, so this is headroom rather than a requirement -- but it
    # becomes required if the repository is ever made private.
    github_token: SecretStr | None = None

    # --- Ingestion behaviour --------------------------------------------------
    # How many files to fetch at once. The limit exists to be polite to
    # raw.githubusercontent.com rather than because anything here is heavy.
    ingestion_concurrency: int = Field(default=5, ge=1, le=20)
    # The purge guard. Ingestion deletes any document it did not see this run,
    # which is how a deleted file leaves the index -- and also exactly how a partial
    # GitHub failure would wipe the knowledge base.
    #
    # The limit is a share of what is stored rather than a fixed count, because a
    # fixed count stops protecting as the corpus grows: a floor of eight guards
    # eleven documents and guards nothing at fifty, where a collapse to nine would
    # clear forty-one and still pass.
    #
    # 0.3 allows a run to remove roughly a third of the knowledge base. Removing
    # more than that in one commit is either a mistake or something worth
    # confirming by hand.
    ingestion_max_purge_fraction: float = Field(default=0.3, ge=0.0, le=1.0)
    # ...except that a share is useless on a small corpus: a third of three
    # documents is one, so removing a single article from a three-article corpus
    # would abort. This many deletions are always allowed, whatever the share.
    ingestion_purge_grace: int = Field(default=2, ge=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # NoDecode turns off the JSON parsing that pydantic-settings applies to list
    # fields by default, so the validator below sees the raw string.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_comma_separated(cls, value: object) -> object:
        """Accept ``a, b`` as a list, since env vars have no notion of a list."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("classifier_reasoning_effort", "chat_reasoning_effort", mode="before")
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """Read ``CHAT_REASONING_EFFORT=`` as "not set" rather than as an invalid value.

        An empty assignment is how a lot of people write "leave this at the default"
        in a .env file. Without this it would fail validation as not one of the four
        allowed words, which is technically correct and helps nobody.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _validate_database_port(self) -> "Settings":
        """Locally, refuse a database URL that is not pointing at pgvector.

        The two local Postgres instances differ only by port: 5432 is plain, and
        5433 is the one with the vector extension. Picking the wrong one connects
        perfectly happily and then fails at the first migration with
        ``type "vector" does not exist`` -- an error that sends you off to debug a
        migration when the real problem is one digit in a URL.

        Enforced only locally. In production 5432 is the ordinary choice, so a
        guard that rejected it everywhere would fail the deployment rather than
        prevent a mistake.

        This runs as a model validator rather than a field validator because it
        needs two fields at once, and a field validator only ever sees its own.
        """
        if self.environment != "local":
            return self
        port = urlparse(self.database_url).port
        if port != _LOCAL_PGVECTOR_PORT:
            raise ValueError(f"Local pgvector runs on port {_LOCAL_PGVECTOR_PORT}.")
        return self


def _describe(error: ValidationError) -> str:
    """Turn a ValidationError into something readable at four in the morning.

    Pydantic reports the Python field name; the person reading the error has to fix
    an environment variable. Translating one to the other is a couple of lines and
    removes a small, repeated moment of friction.
    """
    lines = []
    for item in error.errors():
        field = str(item["loc"][0]) if item["loc"] else "(settings)"
        lines.append(f"  {field.upper()}: {item['msg']}")
    return "Configuration is invalid:\n" + "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    """Return the settings, parsing them on first call and reusing them after.

    The cache is what makes this a singleton. It also makes it *lazy*: settings are
    read when something first asks for them, not when this module is imported, so
    importing the package never depends on the environment being set up. Tests can
    call ``get_settings.cache_clear()`` to force a re-read.
    """
    try:
        return Settings()
    except ValidationError as exc:
        # Re-raised as our own error so callers can catch PortfolioAIError, and so
        # the message names environment variables rather than Pydantic internals.
        # "from exc" keeps the original in the traceback for anyone who needs it.
        raise ConfigError(_describe(exc)) from exc
