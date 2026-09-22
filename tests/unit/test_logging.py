"""The log redaction processor.

A processor is an ordinary function taking a dictionary and returning one, which
makes it unusually easy to test: call it, look at what comes back. No logging
needs configuring and nothing is written anywhere.
"""

import pytest

from portfolio_ai.logging import _redact_secrets


def _redact(event: dict[str, object]) -> dict[str, object]:
    """Call the processor the way structlog would."""
    return _redact_secrets(None, "info", event)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "apikey",
        "openai_api_key",
        "private_key",
        "signing_key",
        "token",
        "access_token",
        "password",
        "secret",
        "authorization",
    ],
)
def test_sensitive_fields_are_masked(field: str) -> None:
    assert _redact({field: "SENSITIVE"})[field] == "***"


@pytest.mark.parametrize(
    "field",
    [
        "key",  # an ordinary word: a dictionary key, a sort key, a cache key
        "monkey",  # the reason the rule requires the underscore
        "keyboard_layout",
        "turkey_recipe",
        "count",
        "user_id",
    ],
)
def test_ordinary_fields_are_left_alone(field: str) -> None:
    """Over-matching is the failure that gets redaction switched off.

    A rule that masks `monkey` fills the logs with asterisks where useful data
    should be, and the next person turns the whole thing off. These cases are
    the ones that keep it usable.
    """
    assert _redact({field: "ordinary value"})[field] == "ordinary value"


def test_matching_ignores_case() -> None:
    """An Authorization header arrives capitalised."""
    assert _redact({"Authorization": "Bearer xyz"})["Authorization"] == "***"


def test_the_event_dictionary_is_returned() -> None:
    """A processor that returns nothing turns the whole log line into `null`.

    Nothing raises, nothing warns -- the entry is simply lost. Worth an explicit
    test, because the failure is invisible in every other way.
    """
    assert _redact({"event": "something_happened", "count": 11}) is not None


def test_parametrize_reports_the_failing_case() -> None:
    """Not a test of the code -- a note about the decorator above.

    @pytest.mark.parametrize runs the same function once per value, and each one
    is reported separately: test_sensitive_fields_are_masked[api_key] rather than
    a single test that stops at whichever case failed first. A loop inside one
    test would hide every case after the first failure.
    """
