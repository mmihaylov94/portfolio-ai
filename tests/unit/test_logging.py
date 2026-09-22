"""The log redaction processor.

A processor is an ordinary function taking a dictionary and returning one, which
makes it unusually easy to test: call it, look at what comes back. No logging
needs configuring and nothing is written anywhere.
"""

import pytest
from structlog.typing import EventDict

from portfolio_ai.logging import _redact_secrets


def _redact(event: EventDict) -> EventDict:
    """Call the processor the way structlog would.

    Annotated with structlog's own EventDict rather than dict[str, object], which
    is what this said first and is not the same thing. EventDict is a
    MutableMapping, so promising to return a dict was a promise the processor never
    made -- and the `# type: ignore` that used to sit on this line was silencing a
    different error entirely, which is why it needed the error code removing before
    anyone could see that.
    """
    return _redact_secrets(None, "info", event)


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


@pytest.mark.parametrize("field", ["total_tokens", "prompt_tokens", "token_count"])
def test_token_counts_are_not_mistaken_for_tokens(field: str) -> None:
    """A regression test for a bug that shipped and ran.

    `token` was matched as a plain substring, so the first ingestion run logged
    `"total_tokens": "***"` -- and every OpenAI call in the project reported its
    usage as three asterisks. Nothing failed; the cost reporting just silently
    returned nothing, which is exactly the over-matching failure the processor's
    own docstring warned about.
    """
    assert _redact({field: 14363})[field] == 14363


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
