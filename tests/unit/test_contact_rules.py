"""The contact form's rules, held to the JavaScript they were ported from.

``contact/rules.py`` replaces a Code node of an n8n workflow. The fixture beside this
file, ``fixtures/contact_rules.json``, is what that node's own code says about
seventy-odd invented submissions: ``fixtures/contact_rules.js`` runs it under Node
and writes the verdicts down. Every case here asks the Python port for the same
verdict, field for field.

That is the whole argument for the port being faithful. The cases are chosen where
the two languages disagree when left to themselves: what white space is, how long a
string with an emoji in it is, what ``\\b`` and a case-insensitive match mean outside
ASCII, and where a string ends.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from portfolio_ai.contact import rules
from portfolio_ai.contact.rules import Checked

FIXTURE = Path(__file__).parent / "fixtures" / "contact_rules.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))

LONG = "I would like to talk about a project for my company next month."


def _check(**fields: str) -> Checked:
    given = {
        "email": "visitor@example.com",
        "subject": "A new project",
        "message": LONG,
        "reason": "project",
        **fields,
    }
    return rules.check(**given)


def test_the_fixture_is_there_and_covers_every_outcome() -> None:
    """pytest skips a test parametrised over an empty list; it does not fail it. And a
    fixture with no spam in it would let the spam threshold be anything."""
    outcomes = {
        "invalid"
        if not case["expected"]["is_valid"]
        else "spam"
        if case["expected"]["is_spam_rule"]
        else "suspicious"
        if case["expected"]["suspicious"]
        else "clean"
        for case in CASES
    }

    assert len(CASES) >= 70
    assert outcomes == {"invalid", "spam", "suspicious", "clean"}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_the_port_gives_the_verdict_n8ns_own_code_gave(case: dict[str, Any]) -> None:
    expected = case["expected"]

    checked = rules.check(**case["input"])

    assert {
        "email": checked.email,
        "subject": checked.subject,
        "message": checked.message,
        "reason": checked.reason,
        "is_valid": checked.is_valid,
        "is_spam_rule": checked.is_spam_rule,
        "suspicious": checked.suspicious,
        "spam_score": checked.spam_score,
        "rule_hits": list(checked.rule_hits),
        "block_reason": checked.block_reason,
    } == expected


def test_the_fixture_holds_no_character_a_reviewer_cannot_see() -> None:
    """Its cases are about zero-width characters and odd spaces. Written as they
    are, nobody could tell one case from another in a diff, so the generator writes
    everything outside printable ASCII as an escape."""
    raw = FIXTURE.read_bytes()

    assert all(byte in {0x09, 0x0A} or 0x20 <= byte <= 0x7E for byte in raw)


# --- where the port and the original part company, on purpose -------------------------


def test_a_word_that_is_also_a_javascript_property_counts_like_any_other() -> None:
    """The one known difference. The original counted words in a plain JavaScript
    object, where ``counts["constructor"]`` is already something before any word is
    counted: a function every object inherits. So "constructor", written four
    times, was never counted as repeated, by accident. A dictionary has no such
    words, and the port does not imitate the accident."""
    message = "constructor destructor constructor destructor constructor destructor " * 2

    assert "Excessive repetition" in _check(message=message).rule_hits


# --- what the rest of the flow reads off a verdict -------------------------------------


def test_blocked_means_invalid_or_spam_and_not_merely_suspicious() -> None:
    invalid = _check(email="not-an-address")
    spam = _check(email="someone@mailinator.com", message="seo??")
    suspicious = _check(message="seo??")
    clean = _check()

    assert [verdict.blocked for verdict in (invalid, spam, suspicious, clean)] == [
        True,
        True,
        False,
        False,
    ]
    assert (suspicious.suspicious, spam.suspicious) == (True, False), "spam is past suspicious"


def test_the_reasons_are_the_five_the_sites_form_offers() -> None:
    assert {"project", "collaboration", "job", "consultation", "other"} == rules.REASONS
