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

    # SecretStr rather than str: see the lesson. Printing this gives "**********".
    openai_api_key: SecretStr

    embedding_model: str = "text-embedding-3-small"
    # Changing either of these invalidates every vector already stored, so they are
    # configuration in the sense of "recorded", not "tweakable".
    embedding_dimensions: int = Field(default=1536, ge=1)

    retrieval_top_k: int = Field(default=20, ge=1, le=100)

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
        if port != 5433:
            raise ValueError("Local pgvector runs on port 5433.")
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
