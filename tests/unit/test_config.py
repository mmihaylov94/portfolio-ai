"""Settings parsing and validation.

Unit tests: no database, no network, no files. Every one of these runs in
milliseconds, which is what makes the default suite worth running constantly.

Note ``_env_file=None`` throughout. Without it, Settings would read the real
``.env``, and the test would pass or fail depending on what happens to be in it
-- which is not a test, it is a coin toss.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from portfolio_ai.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

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
    """extra='forbid'. A typo should stop the process, not be quietly ignored -- and
    the error names the key without repeating its value, which is often a secret."""
    with pytest.raises(ValidationError) as caught:
        _settings(DATABSE_URL="postgresql://u:hunter2@host:5433/db")

    assert "DATABSE_URL" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_environment_must_be_one_of_the_known_values() -> None:
    with pytest.raises(ValidationError):
        _settings(environment="prod")


def test_retrieval_top_k_is_bounded() -> None:
    with pytest.raises(ValidationError):
        _settings(retrieval_top_k=500)


def test_assistant_defaults_are_the_n8n_settings() -> None:
    settings = _settings()

    assert settings.chat_model == "gpt-5-mini"
    assert settings.classifier_model == "gpt-5-mini"
    assert settings.chat_reasoning_effort is None
    assert settings.memory_window_turns == 25


def test_reasoning_effort_accepts_the_known_values() -> None:
    assert _settings(classifier_reasoning_effort="minimal").classifier_reasoning_effort == "minimal"


def test_reasoning_effort_rejects_anything_else() -> None:
    with pytest.raises(ValidationError):
        _settings(chat_reasoning_effort="extreme")


def test_a_blank_reasoning_effort_means_unset() -> None:
    """`CHAT_REASONING_EFFORT=` in a .env file is someone saying "use the default"."""
    assert _settings(chat_reasoning_effort="").chat_reasoning_effort is None


def test_a_validation_error_does_not_repeat_the_value() -> None:
    """hide_input_in_errors. The port guard used to quote every input it was given,
    database password and OpenAI key included, into a traceback the API logs."""
    with pytest.raises(ValidationError) as caught:
        _settings(
            environment="local",
            database_url="postgresql://u:hunter2@host:5432/db",
            openai_api_key="sk-do-not-print-me",
        )

    assert "hunter2" not in str(caught.value)
    assert "sk-do-not-print-me" not in str(caught.value)


# --- the API's settings -------------------------------------------------------


def test_the_api_secrets_are_optional_for_everything_but_the_api() -> None:
    """The worker and the terminal chat run without them; the API refuses to start
    without them, which is tested with the API."""
    settings = _settings()

    assert settings.portfolio_ai_api_key is None
    assert settings.ip_hash_salt is None


def test_a_blank_secret_means_unset() -> None:
    """`PORTFOLIO_AI_API_KEY=` copied from .env.example must not break every command
    by failing the length check."""
    settings = _settings(portfolio_ai_api_key="", ip_hash_salt="   ")

    assert settings.portfolio_ai_api_key is None
    assert settings.ip_hash_salt is None


def test_a_short_secret_is_refused_without_being_repeated() -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(portfolio_ai_api_key="too-short-to-be-a-key")

    message = str(caught.value)
    assert "portfolio_ai_api_key" in message
    assert "at least 32 characters" in message
    assert "too-short-to-be-a-key" not in message


def test_a_long_enough_secret_is_kept_secret() -> None:
    settings = _settings(ip_hash_salt="s" * 64)

    assert settings.ip_hash_salt is not None
    assert "s" * 64 not in repr(settings)


def test_the_spend_cap_is_exact_decimal() -> None:
    """Parsed as Decimal, as the cost_usd column it is compared with."""
    settings = _settings(daily_spend_cap_usd="0.10")

    assert settings.daily_spend_cap_usd == Decimal("0.10")
    assert isinstance(settings.daily_spend_cap_usd, Decimal)


def test_the_spend_cap_cannot_be_negative_but_can_be_zero() -> None:
    """Zero is the switch that turns the chat off; below zero is a typo."""
    assert _settings(daily_spend_cap_usd="0").daily_spend_cap_usd == 0

    with pytest.raises(ValidationError):
        _settings(daily_spend_cap_usd="-1")


def test_env_example_is_a_configuration_that_loads() -> None:
    """The file everyone copies to start must not fail on its first use.

    With extra="forbid", a key in .env.example that is not a setting -- one left
    behind when a setting is removed -- stops every command of anyone who copied it.
    This reads the file as Settings would, so that is caught here instead.
    """
    settings = Settings(_env_file=ENV_EXAMPLE)

    assert settings.portfolio_ai_api_key is None, "blank in the example, so unset"


def test_limits_and_retention_default_to_the_documented_values() -> None:
    settings = _settings()

    assert settings.session_rate_limit == 20
    assert settings.session_rate_window_minutes == 15
    assert settings.daily_spend_cap_usd == Decimal("1.00")
    assert settings.chat_retention_days == 90


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
