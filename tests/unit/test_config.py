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

from portfolio_ai.config import Settings, effort_problem

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


def test_assistant_defaults_are_the_measured_settings() -> None:
    settings = _settings()

    # The answers come from gpt-6-luna; the routing stays with gpt-5-mini, which does it
    # as well and about half a second sooner. Each has the lowest effort it takes.
    assert (settings.chat_model, settings.chat_reasoning_effort) == ("gpt-6-luna", "none")
    assert (settings.classifier_model, settings.classifier_reasoning_effort) == (
        "gpt-5-mini",
        "minimal",
    )
    # The judge is the ruler every stored score was read with. A changed default is a
    # new ruler, and docs/EVALS.md says what has to be graded again first.
    assert settings.judge_model == "gpt-6.1-sol"
    assert settings.memory_window_turns == 25


def test_reasoning_effort_accepts_the_known_values() -> None:
    assert _settings(classifier_reasoning_effort="low").classifier_reasoning_effort == "low"


@pytest.mark.parametrize(
    ("overrides", "named"),
    [
        ({"chat_reasoning_effort": "minimal"}, "CHAT_REASONING_EFFORT"),
        ({"chat_model": "gpt-5-mini"}, "CHAT_REASONING_EFFORT"),
        ({"classifier_reasoning_effort": "none"}, "CLASSIFIER_REASONING_EFFORT"),
        ({"classifier_model": "gpt-6-luna"}, "CLASSIFIER_REASONING_EFFORT"),
    ],
    ids=[
        "chat-effort-from-the-old-model",
        "chat-model-without-its-effort",
        "classifier-effort-from-the-other-model",
        "classifier-model-without-its-effort",
    ],
)
def test_an_effort_its_model_does_not_take_is_refused_at_startup(
    overrides: dict[str, str], named: str
) -> None:
    """gpt-5-mini's lowest effort is "minimal" and gpt-6-luna's is "none", and OpenAI
    refuses each for the other. Changing one of the pair in .env and not the other
    would fail every visitor's question; this fails the process at startup instead."""
    with pytest.raises(ValidationError, match=f"{named}: .* does not take the effort"):
        _settings(**overrides)


def test_two_wrong_pairs_are_reported_together() -> None:
    """One restart to learn of both, rather than one each."""
    with pytest.raises(ValidationError) as caught:
        _settings(
            chat_model="gpt-6-luna",
            chat_reasoning_effort="minimal",
            classifier_model="gpt-6-luna",
            classifier_reasoning_effort="minimal",
        )

    message = str(caught.value)
    assert "CHAT_REASONING_EFFORT: gpt-6-luna does not take" in message
    assert "CLASSIFIER_REASONING_EFFORT: gpt-6-luna does not take" in message


def test_a_dated_model_name_is_checked_like_the_model_it_is() -> None:
    """OpenAI serves each model under a dated name too. The price table already reads
    one as its model; the effort check has to, or the dated name is a way past it."""
    assert effort_problem("gpt-6-luna-2026-09-15", "minimal") is not None
    assert effort_problem("gpt-6-luna-2026-09-15", "none") is None
    with pytest.raises(ValidationError, match="does not take the effort"):
        _settings(chat_model="gpt-6-luna-2026-09-15", chat_reasoning_effort="minimal")


def test_a_model_this_project_has_not_used_is_not_checked() -> None:
    assert _settings(chat_model="some-new-model", chat_reasoning_effort="minimal").chat_model


def test_the_effort_none_is_a_value_and_not_the_absence_of_one() -> None:
    """GPT-6 models take "none" where gpt-5-mini takes "minimal". It is a string that is
    sent; leaving the setting out, or blank, sends nothing and gets the model's default."""
    assert _settings(chat_reasoning_effort="none").chat_reasoning_effort == "none"
    assert _settings(chat_reasoning_effort="").chat_reasoning_effort is None


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


def test_the_mail_settings_default_to_gmails_server_and_to_nobody() -> None:
    """The server is Gmail's unless told otherwise. Who sends, with what password and
    to whom has no default: those are somebody's, and belong in `.env` only."""
    settings = _settings()

    assert (settings.smtp_host, settings.smtp_port) == ("smtp.gmail.com", 587)
    assert settings.smtp_user is None
    assert settings.smtp_app_password is None
    assert settings.digest_to_email is None


@pytest.mark.parametrize(
    "name", ["smtp_user", "smtp_app_password", "digest_to_email", "contact_to_email"]
)
def test_a_blank_mail_setting_means_unset(name: str) -> None:
    """`.env.example` lists all three with nothing after the equals sign. Copied as
    they are, they have to mean "no email", not "an address that is an empty string"."""
    assert getattr(_settings(**{name: "  "}), name) is None


@pytest.mark.parametrize("name", ["smtp_user", "digest_to_email", "contact_to_email"])
@pytest.mark.parametrize(
    "value",
    [
        "owner",
        "owner@localhost",
        "@example.org",
        "ow ner@example.com",
        "owner@example.com\nBcc: someone@example.com",
        "jos\N{LATIN SMALL LETTER E WITH ACUTE}@example.com",
        # Each passed the shape check and was refused by mail.py at the first send: the
        # mail library reads every one of them as something other than itself.
        '"quoted"@example.com',
        "owner@example.com.",
        "a,b@example.com",
        "Owner<owner@example.com>",
    ],
    ids=[
        "no-domain",
        "no-dot",
        "no-name",
        "a-space",
        "a-second-header",
        "not-ascii",
        "quoted",
        "a-trailing-dot",
        "two-addresses",
        "a-name-and-brackets",
    ],
)
def test_what_is_plainly_not_an_address_is_refused_without_being_repeated(
    name: str, value: str
) -> None:
    """The last two would otherwise fail late, and oddly. A value with a line break
    is refused by EmailMessage when the message is built, which for the digest is a
    Monday morning. A letter outside ASCII makes smtplib raise UnicodeEncodeError at
    the login, before it has contacted anyone."""
    with pytest.raises(ValidationError) as caught:
        _settings(**{name: value})

    said = str(caught.value)
    assert "must be an email address" in said
    # Part by part. An error that did repeat the value would print its repr, where a
    # line break is a backslash and an "n", and the whole value would not be found.
    for part in value.splitlines():
        assert part not in said


def test_a_real_looking_address_and_a_password_are_kept() -> None:
    # Made up, in the shape Gmail gives an app password.
    made_up = "abcd efgh ijkl mnop"
    settings = _settings(
        smtp_user="sender@example.com",
        smtp_app_password=made_up,
        digest_to_email="owner@example.com",
    )

    assert (settings.smtp_user, settings.digest_to_email) == (
        "sender@example.com",
        "owner@example.com",
    )
    assert settings.smtp_app_password is not None
    assert "abcd efgh" not in repr(settings), "a SecretStr, like the OpenAI key"
    assert settings.smtp_app_password.get_secret_value() == made_up


def test_the_contact_form_has_nobody_to_tell_until_it_is_given_somebody() -> None:
    """Unset by default, like the digest's recipient: configuration, and never an
    address written into this repository. Without it the endpoint answers 503."""
    assert _settings().contact_to_email is None
    assert _settings(contact_to_email="owner@example.com").contact_to_email == "owner@example.com"


def test_the_contact_forms_hourly_limit_is_thirty_and_at_least_one() -> None:
    """Thirty an hour for the whole service by default. Nought is refused rather
    than read as "off": the form is switched off by leaving its recipient unset,
    and a limit that admits nothing would look like a fault."""
    assert _settings().contact_max_per_hour == 30
    assert _settings(contact_max_per_hour=1).contact_max_per_hour == 1
    with pytest.raises(ValidationError):
        _settings(contact_max_per_hour=0)
    with pytest.raises(ValidationError):
        _settings(contact_max_per_hour=1001)


def test_the_contact_forms_replies_are_ten_a_day_and_nought_switches_them_off() -> None:
    """Unlike the hourly limit, nought is allowed here and means something: the
    messages still come in and reach the owner, and none is answered automatically."""
    assert _settings().contact_max_replies_per_day == 10
    assert _settings(contact_max_replies_per_day=0).contact_max_replies_per_day == 0
    with pytest.raises(ValidationError):
        _settings(contact_max_replies_per_day=-1)
    with pytest.raises(ValidationError):
        _settings(contact_max_replies_per_day=1001)


def test_the_mail_port_is_a_port() -> None:
    assert _settings(smtp_port=1).smtp_port == 1
    assert _settings(smtp_port=65535).smtp_port == 65535
    for not_a_port in (0, 65536):
        with pytest.raises(ValidationError):
            _settings(smtp_port=not_a_port)


def test_the_analytics_thresholds_default_to_the_documented_values() -> None:
    """Provisional figures, and written down in four places: here, config.py,
    .env.example and docs/ANALYTICS.md. This is the one that fails when they part."""
    settings = _settings()

    assert settings.low_score_threshold == pytest.approx(0.35)
    assert settings.rephrase_similarity == pytest.approx(0.60)
    assert settings.gap_similarity == pytest.approx(0.45)


@pytest.mark.parametrize("name", ["low_score_threshold", "rephrase_similarity", "gap_similarity"])
@pytest.mark.parametrize("value", [-0.1, 1.1])
def test_a_threshold_is_a_similarity_between_nought_and_one(name: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _settings(**{name: value})


@pytest.mark.parametrize("name", ["low_score_threshold", "rephrase_similarity", "gap_similarity"])
@pytest.mark.parametrize("value", [0.0, 1.0])
def test_nought_and_one_themselves_are_allowed(name: str, value: float) -> None:
    """Each end switches a list off or fully on: no answer scores under 0, and no two
    questions are more alike than 1."""
    assert getattr(_settings(**{name: value}), name) == pytest.approx(value)


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
