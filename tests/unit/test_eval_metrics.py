"""Retrieval scores and the deterministic rules: arithmetic and patterns, no model."""

import itertools
from typing import Any

import pytest

from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.evals import metrics
from portfolio_ai.evals.datasets import EvalCase

_IDS = itertools.count(1)


def _chunk(doc_id: str, *, url: str | None = None, content: str = "Text.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=next(_IDS),
        doc_id=doc_id,
        title=doc_id.title(),
        url=url,
        section="main",
        section_title="Main",
        content=content,
        score=0.5,
    )


def _case(**overrides: Any) -> EvalCase:
    values: dict[str, Any] = {
        "key": "case",
        "question": "What is Mihail's job title?",
        "category": "mihail_related",
        "expected_doc_ids": ("about-mihail",),
        **overrides,
    }
    return EvalCase.model_validate(values)


def _rules(answer: str, *, case: EvalCase | None = None, **overrides: Any) -> metrics.Violations:
    arguments: dict[str, Any] = {
        "route": "mihail_related",
        "links_removed": 0,
        "chunks": [],
        **overrides,
    }
    return metrics.rule_violations(case or _case(), answer=answer, **arguments)


# --- retrieval ------------------------------------------------------------------


def test_documents_are_ranked_by_first_appearance_without_repeats() -> None:
    chunks = [_chunk("faq"), _chunk("about-mihail"), _chunk("faq", content="More.")]

    assert metrics.ranked_documents(chunks) == ["faq", "about-mihail"]


def test_an_expected_document_found_first_scores_full_marks() -> None:
    scores = metrics.retrieval_scores(["about-mihail"], ["about-mihail", "faq"])

    assert scores == metrics.RetrievalScores(recall=1.0, precision=0.5, mrr=1.0)
    assert scores.hit


def test_mrr_is_one_over_the_rank_of_the_first_expected_document() -> None:
    scores = metrics.retrieval_scores(["hiring", "contact"], ["faq", "about-mihail", "contact"])

    assert scores is not None
    assert scores.mrr == pytest.approx(1 / 3)
    assert scores.recall == pytest.approx(0.5), "one of the two expected documents"


def test_nothing_expected_found_is_a_miss() -> None:
    scores = metrics.retrieval_scores(["tech-stack"], ["faq"])

    assert scores == metrics.RetrievalScores(recall=0.0, precision=0.0, mrr=0.0)
    assert not scores.hit


def test_a_case_expecting_no_documents_has_no_retrieval_score() -> None:
    assert metrics.retrieval_scores([], ["faq"]) is None


def test_nothing_shown_is_zero_precision_rather_than_a_division_by_zero() -> None:
    scores = metrics.retrieval_scores(["faq"], [])

    assert scores is not None
    assert scores.precision == pytest.approx(0.0)


# --- rules ----------------------------------------------------------------------


def test_a_good_answer_breaks_no_rule() -> None:
    answer = (
        "Mihail is a Solutions Architect at Businessmap. Before that he was a Technical "
        "Solution Architect at Deloitte. Want to hear more about either role?"
    )

    assert _rules(answer) == {}


def test_the_wrong_route_is_recorded() -> None:
    assert _rules("Hi!", route="small_talk")["misrouted"] == "small_talk"


def test_a_route_the_case_also_accepts_is_not_misrouted() -> None:
    case = _case(category="small_talk", also_accept=("mihail_related",), expected_doc_ids=())

    assert "misrouted" not in _rules("No, I'm Rachel.", case=case, route="mihail_related")


def test_a_forbidden_link_the_filter_caught_is_still_recorded() -> None:
    """The visitor never saw it, but the model wrote it: the prompt is not holding."""
    assert _rules("See the Threadline page.", links_removed=1)["forbidden_link_attempted"] == 1


def test_the_page_the_filter_put_in_a_forbidden_links_place_is_not_invented() -> None:
    """The attempt is recorded once, as an attempt, and not again as a made-up link."""
    found = _rules("Read more at https://mihaylov.io/#projects today.", links_removed=1)

    assert found == {"forbidden_link_attempted": 1}


def test_a_forbidden_link_in_the_answer_itself_is_recorded() -> None:
    found = _rules("See https://mihaylov.io/projects/threadline for more.")

    assert found["forbidden_link_in_answer"] == 1


def test_a_link_nobody_offered_is_invented() -> None:
    found = _rules("His blog is at https://mihail-blog.example.com/posts.")

    assert found["invented_link"] == ["https://mihail-blog.example.com/posts"]


@pytest.mark.parametrize(
    ("answer", "chunks"),
    [
        ("The live forum is at https://threadline.mihaylov.io.", []),
        (
            "Case study: https://mihaylov.io/case-studies/glotsmith",
            [_chunk("glotsmith", url="https://mihaylov.io/case-studies/glotsmith")],
        ),
        (
            "Repo: https://github.com/mmihaylov94/n8n-lite-automations",
            [
                _chunk(
                    "n8n", content="GitHub: `https://github.com/mmihaylov94/n8n-lite-automations`"
                )
            ],
        ),
        (
            "Use [the contact form](/?section=contact).",
            [_chunk("contact", url="https://mihaylov.io/?section=contact")],
        ),
    ],
    ids=["offered-by-the-prompt", "a-document-url", "in-a-passage", "relative-to-the-site"],
)
def test_a_link_the_prompt_or_the_passages_offered_is_not_invented(
    answer: str, chunks: list[RetrievedChunk]
) -> None:
    assert "invented_link" not in _rules(answer, chunks=chunks)


@pytest.mark.parametrize(
    "answer",
    [
        "Email him: [someone@example.com](mailto:someone@example.com).",
        "He is on LinkedIn at www.linkedin.com/in/mihail-m-mihaylov.",
        "LinkedIn: **https://www.linkedin.com/in/mihail-m-mihaylov**.",
    ],
    ids=["mailto", "no-scheme", "in-bold"],
)
def test_a_link_written_differently_from_the_passage_is_the_same_link(answer: str) -> None:
    """The same page with or without its scheme, www. or Markdown emphasis, and an email
    address as a mailto link, which is a contact detail for the judge rather than a page."""
    contact = _chunk(
        "contact",
        content="Email someone@example.com, or https://www.linkedin.com/in/mihail-m-mihaylov.",
    )

    assert "invented_link" not in _rules(answer, chunks=[contact])


def test_a_relative_link_to_a_page_nobody_offered_is_invented() -> None:
    found = _rules("Use [the hiring form](/?section=hire).", chunks=[_chunk("contact")])

    assert found["invented_link"] == ["/?section=hire"]


def test_required_and_forbidden_phrases_are_matched_without_case() -> None:
    case = _case(must_include=("Sofia", "Bulgaria"), must_not_include=("Glasgow",))

    found = _rules("He lives in sofia and studied in GLASGOW.", case=case)

    assert found["missing"] == ["Bulgaria"]
    assert found["forbidden_phrase"] == ["Glasgow"]


@pytest.mark.parametrize(
    ("answer", "rule"),
    [
        ("## Summary\nHe is a Solutions Architect.", "section_header"),
        ("**Roles**\nSolutions Architect at Businessmap.", "section_header"),
        ("He works with:\n- Vue\n- Nuxt\n- React", "bullet_list"),
        ("He works with:\n1. Vue\n2. Nuxt", "bullet_list"),
        (" ".join(f"Sentence number {n}." for n in range(8)), "too_long"),
        ("Hello! Mihail is a Solutions Architect.", "unprompted_greeting"),
        ("I'm Rachel, and Mihail is a Solutions Architect.", "introduced_self"),
        ("I built Glotsmith on my own.", "spoke_as_mihail"),
        ("My projects include Glotsmith.", "spoke_as_mihail"),
    ],
)
def test_each_style_rule_catches_its_pattern(answer: str, rule: str) -> None:
    assert rule in _rules(answer)


CURLY = "\N{RIGHT SINGLE QUOTATION MARK}"


@pytest.mark.parametrize(
    "answer",
    [
        "No, I'm Mihail's AI assistant, Rachel.",
        f"I{CURLY}m Mihail{CURLY}s website assistant.",
        "I am Mihail's assistant, not Mihail himself.",
    ],
)
def test_saying_whose_assistant_she_is_is_not_speaking_as_mihail(answer: str) -> None:
    """The possessive: "I'm Mihail's" is the right answer to "Are you Mihail?", and
    \\b alone would match it, because an apostrophe is not a word character."""
    who = _case(category="small_talk", expected_doc_ids=(), must_include=("Rachel",))

    assert "spoke_as_mihail" not in _rules(answer, case=who, route="small_talk")


def test_claiming_to_be_mihail_is_speaking_as_him() -> None:
    assert _rules("Yes, I'm Mihail. Ask me anything.")["spoke_as_mihail"] == ["I'm Mihail"]


def test_one_line_starting_with_a_dash_is_not_a_list() -> None:
    assert "bullet_list" not in _rules("He works with Vue -\n- and Nuxt too.")


def test_a_curly_apostrophe_is_read_as_a_straight_one() -> None:
    answer = f"I{CURLY}m Rachel. Mihail is a Solutions Architect."

    assert "introduced_self" in _rules(answer)


def test_greeting_and_introduction_are_right_when_the_case_asks_for_them() -> None:
    small_talk = _case(category="small_talk", expected_doc_ids=())
    who = _case(category="small_talk", expected_doc_ids=(), must_include=("Rachel",))

    assert _rules("Hello! How can I help?", case=small_talk, route="small_talk") == {}
    assert _rules("I'm Rachel, the AI assistant.", case=who, route="small_talk") == {}


# --- the fallback ---------------------------------------------------------------


def test_a_question_the_knowledge_base_cannot_answer_must_be_declined() -> None:
    case = _case(expected_doc_ids=(), expect_fallback=True)

    found = metrics.fallback_violations(
        case, route="mihail_related", fallback_used=False, declined=False
    )

    assert found == {"did_not_decline": True}


def test_the_judges_reading_wins_over_the_phrase_match() -> None:
    """A decline worded differently: the phrase match misses it, the judge does not."""
    case = _case(expected_doc_ids=(), expect_fallback=True)

    assert (
        metrics.fallback_violations(
            case, route="mihail_related", fallback_used=False, declined=True
        )
        == {}
    )


def test_without_a_judge_the_phrase_match_decides() -> None:
    case = _case(expected_doc_ids=(), expect_fallback=True)

    assert (
        metrics.fallback_violations(case, route="mihail_related", fallback_used=True, declined=None)
        == {}
    )


def test_declining_an_answerable_question_is_recorded() -> None:
    found = metrics.fallback_violations(
        _case(), route="mihail_related", fallback_used=False, declined=True
    )

    assert found == {"declined_answerable": True}
