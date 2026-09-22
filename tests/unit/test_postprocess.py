"""The link rule and the fallback check.

The streaming filter gets a property test rather than a handful of examples. The
claim it makes is "however the text is split, the result is the same as cleaning it
all at once", and the way to test a claim about *every* split is to try every split
-- of every sample, at every position -- plus a few hundred random multi-way ones.
"""

import random
import re

import pytest

from portfolio_ai.assistant.postprocess import (
    LinkFilter,
    is_fallback,
    is_forbidden_url,
    strip_forbidden_links,
)
from portfolio_ai.assistant.prompts.loader import RAG_AGENT

# Each one is here because it has broken, or could break, a naive version.
SAMPLES = [
    "See https://mihaylov.io/projects/threadline for details.",
    "Read [the case study](https://mihaylov.io/projects/threadline) or "
    "[contact him](https://mihaylov.io/#contact).",
    "Allowed: https://mihaylov.io/#about, https://threadline.mihaylov.io and "
    "www.linkedin.com/in/mihail-m-mihaylov.",
    "A label with spaces: [Threadline project page](https://mihaylov.io/projects/threadline).",
    "Brackets that are not links: [1] and [note], then (a parenthetical).",
    "An unclosed [bracket that never closes, then https://mihaylov.io/knowledgebase/faq.md",
    "<https://mihaylov.io/projects/x> and <https://mihaylov.io/>",
    "Shouting: HTTPS://MIHAYLOV.IO/PROJECTS/THREADLINE.",
    "[https://mihaylov.io/projects/x](https://mihaylov.io/projects/x)",
    "Nested [a [b](https://mihaylov.io/projects/y) c",
    "Lines:\n- one https://mihaylov.io/projects/a\n- [two](https://github.com/mmihaylov94/threadline)\n",
    "Touching: https://mihaylov.io/projects/x[link](https://mihaylov.io/projects/y) end",
    "In brackets (https://mihaylov.io/projects/threadline) mid-sentence.",
    "x" * 40 + "https://mihaylov.io/projects/q",
    "Mihail\u2019s site \u2014 https://mihaylov.io/#about \u2713 and nothing forbidden at all.",
    "",
]


def _stream(pieces: list[str]) -> tuple[str, int]:
    """Feed the pieces through a filter, return what came out and how much went."""
    link_filter = LinkFilter()
    out = "".join(link_filter.feed(piece) for piece in pieces) + link_filter.finish()
    return out, link_filter.removed


# --- the one-shot rule ------------------------------------------------------


def test_a_bare_forbidden_url_is_removed_and_its_full_stop_kept() -> None:
    text, removed = strip_forbidden_links("See https://mihaylov.io/projects/threadline.")

    assert text == "See ."
    assert removed == 1


def test_a_markdown_link_to_a_forbidden_url_keeps_its_label() -> None:
    text, _ = strip_forbidden_links("Read [the case study](https://mihaylov.io/projects/x) now")

    assert text == "Read the case study now"


def test_allowed_links_are_left_exactly_as_written() -> None:
    allowed = (
        "See [About](https://mihaylov.io/#about), https://threadline.mihaylov.io and "
        "https://github.com/mmihaylov94/threadline."
    )

    assert strip_forbidden_links(allowed) == (allowed, 0)


def test_the_knowledgebase_path_is_forbidden_too() -> None:
    text, removed = strip_forbidden_links("From https://mihaylov.io/knowledgebase/faq.md today")

    assert "knowledgebase" not in text
    assert removed == 1


def test_a_label_that_is_itself_a_forbidden_url_goes_as_well() -> None:
    text, _ = strip_forbidden_links(
        "[https://mihaylov.io/projects/x](https://mihaylov.io/projects/x)"
    )

    assert not text


def test_matching_ignores_case() -> None:
    assert is_forbidden_url("HTTPS://MIHAYLOV.IO/PROJECTS/THREADLINE")
    assert not is_forbidden_url("https://mihaylov.io/#projects")


# --- streaming --------------------------------------------------------------


@pytest.mark.parametrize("sample", SAMPLES)
def test_every_two_way_split_gives_the_one_shot_result(sample: str) -> None:
    expected = strip_forbidden_links(sample)

    for position in range(len(sample) + 1):
        assert _stream([sample[:position], sample[position:]]) == expected, position


@pytest.mark.parametrize("sample", SAMPLES)
def test_one_character_at_a_time_gives_the_one_shot_result(sample: str) -> None:
    assert _stream(list(sample)) == strip_forbidden_links(sample)


@pytest.mark.parametrize("sample", SAMPLES)
def test_random_splits_give_the_one_shot_result(sample: str) -> None:
    # Seeded, so a failure reproduces exactly. Bandit's rule about the random
    # module is about secrets; this is test data.
    rng = random.Random(sample)  # ruff: ignore[suspicious-non-cryptographic-random-usage]
    expected = strip_forbidden_links(sample)

    for _ in range(200):
        pieces, rest = [], sample
        while rest:
            size = rng.randint(1, 8)
            pieces.append(rest[:size])
            rest = rest[size:]

        assert _stream(pieces) == expected, pieces


def test_text_before_the_word_in_progress_is_sent_immediately() -> None:
    """The filter must hold back as little as possible, or streaming is pointless."""
    link_filter = LinkFilter()

    assert link_filter.feed("Mihail builds Lara") == "Mihail builds "
    assert link_filter.feed("vel apps.") == "Laravel "
    assert link_filter.finish() == "apps."


def test_an_open_markdown_link_is_held_until_it_closes() -> None:
    link_filter = LinkFilter()

    assert link_filter.feed("See [the case study](https://mihaylov.io/pro") == "See "
    assert link_filter.feed("jects/x) today") == "the case study "
    assert link_filter.finish() == "today"


# --- the fallback answer ----------------------------------------------------


def test_the_prompts_own_fallback_sentence_is_recognised() -> None:
    """If rag_agent.md's fallback wording changes, this fails until the matcher
    in postprocess.py is updated to recognise it."""
    match = re.search(r'"(I don.t have that information[^"]*)"', RAG_AGENT.text)

    assert match, "rag_agent.md no longer contains the fallback sentence"
    assert is_fallback(match.group(1))


def test_a_straight_apostrophe_version_is_recognised() -> None:
    assert is_fallback("I don't have that information in my knowledge base.")


def test_the_not_enough_information_wording_is_recognised() -> None:
    assert is_fallback("I do not have enough information to answer that.")


def test_an_ordinary_answer_is_not_a_fallback() -> None:
    assert not is_fallback("Yes, Laravel is the PHP framework Mihail uses most.")
