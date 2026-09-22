"""Settings parsing and validation.

Unit tests: no database, no network, no files. Every one of these runs in
milliseconds, which is what makes the default suite worth running constantly.

Note ``_env_file=None`` throughout. Without it, Settings would read the real
``.env``, and the test would pass or fail depending on what happens to be in it
-- which is not a test, it is a coin toss.
"""

import pytest
from pydantic import ValidationError

from portfolio_ai.config import Settings

# Enough to construct a valid Settings. Individual tests override one field to
# check one behaviour, rather than repeating the whole dictionary each time.
VALID = {
    "environment": "local",
    "database_url": "postgresql://u:p@host:5433/db",
    "openai_api_key": "sk-not-a-real-key",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**VALID, **overrides})  # type: ignore[arg-type]


def test_loads_with_defaults() -> None:
    settings = _settings()

    assert settings.db_schema == "portfolio_rag"
    assert settings.retrieval_top_k == 20
    assert settings.embedding_dimensions == 1536


def test_secret_is_masked_when_printed() -> None:
    """The point of SecretStr: the unsafe thing has to be asked for explicitly."""
    settings = _settings()

    assert "sk-not-a-real-key" not in str(settings)
    assert "sk-not-a-real-key" not in repr(settings)
    assert settings.openai_api_key.get_secret_value() == "sk-not-a-real-key"


def test_unknown_key_is_rejected() -> None:
    """extra='forbid'. A typo should stop the process, not be quietly ignored."""
    with pytest.raises(ValidationError) as caught:
        _settings(DATABSE_URL="postgresql://u:p@host:5433/db")

    assert "DATABSE_URL" in str(caught.value)


def test_environment_must_be_one_of_the_known_values() -> None:
    with pytest.raises(ValidationError):
        _settings(environment="prod")


def test_retrieval_top_k_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _settings(retrieval_top_k=500)


def test_cors_origins_splits_on_commas() -> None:
    """Environment variables have no notion of a list, so one gets unpacked."""
    settings = _settings(cors_origins="https://a.example, https://b.example")

    assert settings.cors_origins == ["https://a.example", "https://b.example"]


# --- the port guard from lesson 3 -------------------------------------------
#
# Written as one test per case rather than a loop, so a failure names the case
# that failed instead of "the parametrised one".


def test_local_rejects_the_port_without_pgvector() -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(environment="local", database_url="postgresql://u:p@host:5432/db")

    assert "5433" in str(caught.value)


def test_local_accepts_the_pgvector_port() -> None:
    assert _settings(environment="local", database_url="postgresql://u:p@host:5433/db")


def test_production_accepts_the_standard_port() -> None:
    """The case that makes the guard correct rather than merely strict.

    In production 5432 is the ordinary choice. A validator that rejected it
    everywhere would pass every local test and break the deployment.
    """
    settings = _settings(environment="production", database_url="postgresql://u:p@host:5432/db")

    assert settings.environment == "production"
