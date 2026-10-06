"""The link rule and the fallback check.

The streaming filter gets a property test rather than a handful of examples. The
claim it makes is "however the text is split, the result is the same as cleaning it
all at once", and the way to test a claim about *every* split is to try every split
-- of every sample, at every position -- plus a few hundred random multi-way ones.
"""

import random
import re
import time

import pytest

from portfolio_ai.assistant.postprocess import (
    HOME_PAGE,
    PROJECTS_PAGE,
    LinkFilter,
    is_fallback,
    is_forbidden_url,
    strip_forbidden_links,
    urls_in,
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
    "Bold **https://mihaylov.io/projects/threadline**. Italic _https://mihaylov.io/knowledgebase/"
    "faq.md_, and **(https://mihaylov.io/projects/x.)**",
    'Titled: [the case study](https://mihaylov.io/projects/x "The case study") and '
    "[about](https://mihaylov.io/#about 'About him') end, [open](https://mihaylov.io/ \"x",
    "Elsewhere: https://github.com/u/projects/1, then https://mihaylov.io/projects and "
    "https://mihaylov.io/projects?tab=2 done.",
    # A kept label joining the text after it, and the URL before it, into a new
    # forbidden URL; the second with a space in the label, so a stream holds the
    # link back mid-label while the URL before it is already complete.
    "[https://mihaylov.io](https://mihaylov.io/projects/x)/projects/x and on",
    "See https://mihaylov.io[/projects page](https://github.com/users/mmihaylov94/projects/1) now.",
    "Glued [https://mihaylov.io](https://github.com/u/projects/1)[/projects page]"
    "(https://github.com/u/projects/2) end",
    "Stand-in: https://mihaylov.io/projects/x[/knowledgebase](https://mihaylov.io/projects/y) end",
    # A link that only a removal creates, from a "[" that is not a link as written:
    # an empty-label link, a deleted URL, or a removed link's own "[" goes, and
    # "[a b](...)" is left. Cut at the space in "a b", a stream let it through.
    "[a b][](https://example.com/projects/1)(https://mihaylov.io/x'/../projects/x) end",
    "[a b]https://github.com/u/projects/1(https://mihaylov.io/x'/../projects/x) end",
    "[[](https://example.com/projects/1)a b](https://mihaylov.io/x'/../projects/x) end",
    "[[a](https://example.com/projects/1) b](https://mihaylov.io/x'/../projects/x) end",
    "",
]


def _stream(pieces: list[str]) -> tuple[str, int]:
    """Feed the pieces through a filter, return what came out and how much went."""
    link_filter = LinkFilter()
    out = "".join(link_filter.feed(piece) for piece in pieces) + link_filter.finish()
    return out, link_filter.removed


# --- the one-shot rule ------------------------------------------------------


def test_a_bare_forbidden_url_becomes_the_projects_section_and_its_full_stop_stays() -> None:
    """Deleting it left "See ." behind; a real page in its place keeps the sentence."""
    text, removed = strip_forbidden_links("See https://mihaylov.io/projects/threadline.")

    assert text == "See https://mihaylov.io/#projects."
    assert removed == 1


@pytest.mark.parametrize(
    ("written", "shown"),
    [
        ("at **https://mihaylov.io/projects/x**.", "at **https://mihaylov.io/#projects**."),
        ("at *https://mihaylov.io/projects/x*!", "at *https://mihaylov.io/#projects*!"),
        ("at __https://mihaylov.io/projects/x__", "at __https://mihaylov.io/#projects__"),
        ("at **https://mihaylov.io/projects/x.**", "at **https://mihaylov.io/#projects.**"),
        ("at <https://mihaylov.io/projects/x>.", "at <https://mihaylov.io/#projects>."),
    ],
    ids=["bold", "italic", "underscores", "full-stop-inside", "angle-brackets"],
)
def test_what_surrounds_a_forbidden_url_stays_around_its_stand_in(written: str, shown: str) -> None:
    """The stray "**" a deleted bold link used to leave behind."""
    assert strip_forbidden_links(written) == (shown, 1)


def test_a_markdown_link_to_a_forbidden_url_keeps_its_label() -> None:
    text, _ = strip_forbidden_links("Read [the case study](https://mihaylov.io/projects/x) now")

    assert text == "Read the case study now"


def test_allowed_links_are_left_exactly_as_written() -> None:
    allowed = (
        "See [About](https://mihaylov.io/#about), https://threadline.mihaylov.io and "
        "https://github.com/mmihaylov94/threadline."
    )

    assert strip_forbidden_links(allowed) == (allowed, 0)


def test_the_knowledgebase_path_is_forbidden_too_and_becomes_the_home_page() -> None:
    text, removed = strip_forbidden_links("From https://mihaylov.io/knowledgebase/faq.md today")

    assert text == "From https://mihaylov.io/ today"
    assert removed == 1


def test_a_label_that_is_itself_a_forbidden_url_is_replaced_as_well() -> None:
    text, removed = strip_forbidden_links(
        "[https://mihaylov.io/projects/x](https://mihaylov.io/projects/x)"
    )

    assert text == "https://mihaylov.io/#projects"
    assert removed == 2, "the label's URL and the link's target"


def test_another_sites_forbidden_url_is_dropped_not_replaced() -> None:
    """A sentence about GitHub must not end up pointing at the site."""
    text, removed = strip_forbidden_links(
        "His GitHub project board is at https://github.com/users/mmihaylov94/projects/1."
    )

    assert text == "His GitHub project board is at ."
    assert removed == 1


def test_a_titled_markdown_link_keeps_its_label_like_any_other() -> None:
    """Without the title in the pattern, the URL was read as a bare one, and its label
    ended up pointing at the projects section."""
    forbidden = 'Read [the case study](https://mihaylov.io/projects/threadline "Threadline") now.'
    allowed = "See [About](https://mihaylov.io/#about 'About him')."

    assert strip_forbidden_links(forbidden) == ("Read the case study now.", 1)
    assert strip_forbidden_links(allowed) == (allowed, 0)


def test_a_label_that_runs_into_the_text_after_it_is_checked_again() -> None:
    """The link goes and its label stays, joined to "/projects/x" into a new forbidden
    URL. One pass of re.sub never looks at its own result, and left that URL in."""
    text, removed = strip_forbidden_links(
        "[https://mihaylov.io](https://mihaylov.io/projects/x)/projects/x"
    )

    assert text == "https://mihaylov.io/#projects"
    assert removed == 2, "the link, then the URL its label made"


def test_a_label_that_joins_the_url_before_it_is_checked_again() -> None:
    text, removed = strip_forbidden_links(
        "See https://mihaylov.io[/projects](https://github.com/users/mmihaylov94/projects/1) now."
    )

    assert text == "See https://mihaylov.io/#projects now."
    assert removed == 2


@pytest.mark.parametrize("sample", SAMPLES)
def test_cleaning_the_result_again_finds_nothing(sample: str) -> None:
    """The guarantee itself: what the filter lets through has no forbidden link left."""
    cleaned, _ = strip_forbidden_links(sample)

    assert strip_forbidden_links(cleaned) == (cleaned, 0)


def test_a_reference_style_definition_is_treated_as_a_bare_url() -> None:
    """A known, accepted gap (see the docstring): answers do not write these."""
    text, removed = strip_forbidden_links("[1]: https://mihaylov.io/projects/threadline")

    assert (text, removed) == ("[1]: https://mihaylov.io/#projects", 1)


def test_the_stand_ins_are_links_the_prompt_itself_offers() -> None:
    """A stand-in has to be a link Rachel may give. If rag_agent.md's list of allowed
    links ever drops one, this fails before a visitor is sent somewhere unvetted."""
    offered = urls_in(RAG_AGENT.text)

    assert PROJECTS_PAGE in offered
    assert HOME_PAGE in offered


def test_matching_ignores_case() -> None:
    assert is_forbidden_url("HTTPS://MIHAYLOV.IO/PROJECTS/THREADLINE")
    assert not is_forbidden_url("https://mihaylov.io/#projects")


@pytest.mark.parametrize(
    ("url", "forbidden"),
    [
        ("https://mihaylov.io/projects", True),
        ("https://mihaylov.io/projects?tab=2", True),
        ("https://mihaylov.io/knowledgebase#faq", True),
        ("https://mihaylov.io/projects-overview", False),
        ("https://mihaylov.io/?section=projects", False),
        ("https://mihaylov.io/#projects", False),
    ],
)
def test_a_forbidden_segment_is_caught_without_its_trailing_slash(
    url: str, *, forbidden: bool
) -> None:
    """ "/projects" at the end of a URL used to slip through a test for "/projects/"."""
    assert is_forbidden_url(url) is forbidden


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


# Pieces from which a removal can build a link the text did not have: something that
# opens one, something a pass removes, then something that closes one.
_OPENERS = [
    "[a b]",
    "[a b",
    "[",
    "[[](https://example.com/projects/1)a b]",
    "[[a](https://example.com/projects/1) b]",
    "https://mihaylov.io[",
]
_REMOVED = [
    "",
    "[](https://example.com/projects/1)",
    "[[](https://example.com/projects/1)",
    "[a](https://example.com/projects/1)",
    "https://github.com/u/projects/1",
    "<https://github.com/u/projects/1>",
]
_CLOSERS = [
    "(https://mihaylov.io/x'/../projects/x)",
    "(https://mihaylov.io/#about)",
    "](https://mihaylov.io/x'/../projects/x)",
    "/projects page](https://github.com/u/projects/2)",
]
_AROUND = ["a", "see", " ", " ", "\n", "https://mihaylov.io", "/projects/x", "**"]


def test_streaming_link_shaped_text_gives_the_one_shot_result() -> None:
    """Hand-picked samples missed a whole family of these; generated ones find it.

    Five hundred texts built to let a removal assemble a new link, streamed one
    character at a time. Against the cut this replaced, which judged links from the
    text as written, 27 of them came out different -- each with a forbidden link in
    it -- and against the version before that, 91.
    """
    rng = random.Random(1)  # ruff: ignore[suspicious-non-cryptographic-random-usage]

    for _ in range(500):
        parts = [rng.choice(_AROUND) for _ in range(rng.randint(0, 2))]
        parts.append(rng.choice(_OPENERS))
        parts += [rng.choice(_REMOVED) for _ in range(rng.randint(1, 2))]
        parts.append(rng.choice(_CLOSERS))
        parts += [rng.choice(_AROUND) for _ in range(rng.randint(0, 2))]
        text = "".join(parts)

        streamed = _stream(list(text))

        assert streamed == strip_forbidden_links(text), text
        assert strip_forbidden_links(streamed[0]) == (streamed[0], 0), text


@pytest.mark.parametrize(
    "text",
    ["[" * 3000, "[a b](https://example.com/c)" * 500],
    ids=["3,000 lone brackets", "500 glued links"],
)
def test_a_piece_costs_the_same_however_much_is_held_back(text: str) -> None:
    """Neither text has a space the filter can cut at, so all of it is held back.

    A filter that looked at the whole held text again for every piece took 18 and
    13 seconds over these, holding the event loop that every visitor's answer
    shares. One that keeps its place takes a few hundredths of a second; a second
    is the margin for a slow machine.
    """
    started = time.perf_counter()

    _stream([text[i : i + 4] for i in range(0, len(text), 4)])

    assert time.perf_counter() - started < 1.0


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


def test_what_is_written_against_an_open_link_waits_with_it() -> None:
    """Sent at once, "https://mihaylov.io" could not be taken back when the link's
    label, "/projects page", joined it."""
    link_filter = LinkFilter()

    assert link_filter.feed("See https://mihaylov.io[/projects pa") == "See "
    assert (
        link_filter.feed("ge](https://github.com/users/mmihaylov94/projects/1) now.")
        == "https://mihaylov.io/#projects page "
    )
    assert link_filter.finish() == "now."


def test_a_bracket_that_is_no_link_holds_its_line_until_the_line_ends() -> None:
    """A "[" that is not a link as written can still become one when something after
    it is removed, so the rest of its line waits. No link reaches past a line end."""
    link_filter = LinkFilter()

    assert link_filter.feed("See [1] and more ") == "See "
    assert link_filter.feed("text.\nNext line ") == "[1] and more text.\nNext line "
    assert not link_filter.finish(), "nothing left to send"


def test_a_finished_link_glued_to_an_open_one_waits_with_it() -> None:
    """Both links go, and their labels join into a forbidden URL. Sent as soon as it
    was finished, the first label could not have been taken back."""
    link_filter = LinkFilter()
    first = "x [https://mihaylov.io](https://github.com/u/projects/1)[/projects pa"

    assert link_filter.feed(first) == "x "
    assert (
        link_filter.feed("ge](https://github.com/u/projects/2) end")
        == "https://mihaylov.io/#projects page "
    )
    assert link_filter.removed == 3


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


# --- listing links, for the evals -----------------------------------------------


def test_every_link_is_listed_as_the_filter_would_see_it() -> None:
    text = (
        "See <https://threadline.mihaylov.io>, the [case study](https://mihaylov.io/case-studies/"
        "glotsmith), www.linkedin.com/in/mihail-m-mihaylov and **https://mihaylov.io/#about**."
    )

    assert urls_in(text) == [
        "https://threadline.mihaylov.io",
        "https://mihaylov.io/case-studies/glotsmith",
        "www.linkedin.com/in/mihail-m-mihaylov",
        "https://mihaylov.io/#about",
    ]


def test_a_link_used_as_a_label_is_listed_as_well_as_its_target() -> None:
    text = "[https://mihaylov.io/#about](https://mihaylov.io/#about)"

    assert urls_in(text) == ["https://mihaylov.io/#about", "https://mihaylov.io/#about"]


def test_text_without_links_lists_none() -> None:
    assert urls_in("Mihail works with [1] Vue and (2) Nuxt.") == []
