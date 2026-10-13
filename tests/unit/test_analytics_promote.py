"""A visitor's question as an eval case: what is built, and what is printed.

No database. The three reads ``promote`` makes are replaced by stand-ins, and the
reads themselves are tested against a real Postgres in
tests/integration/test_analytics_promote.py.

The test that matters most here is the round trip. What ``promote-case`` prints is
pasted into a dataset file by hand and read back by the eval harness, so the printed
text is fed to the same loader a dataset file goes through, and has to come back as
the case it was made from, whatever the visitor typed.
"""

import datetime as dt
from collections.abc import Sequence

import pytest

from portfolio_ai import strict_yaml
from portfolio_ai.analytics import promote
from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db.analytics import AnsweredQuestion
from portfolio_ai.evals import datasets
from portfolio_ai.evals.datasets import Dataset, EvalCase, Turn
from portfolio_ai.exceptions import AnalyticsError

QUESTION = "What does Mihail charge for a day of consulting?"
ASKED = dt.datetime(2026, 10, 12, 9, 30, tzinfo=dt.UTC)
INDEXED = {"services", "faq"}
BEFORE = [
    ("What does he do?", "He is a Solutions Architect."),
    ("Does he take on consulting?", "Yes, he does. Would you like to know how?"),
]


class Stored:
    """Stand-ins for the three reads, recording what each was asked."""

    def __init__(self) -> None:
        self.question: AnsweredQuestion | None = AnsweredQuestion(
            question_id=812,
            question=QUESTION,
            asked_at=ASKED,
            session_pk=41,
            route="mihail_related",
            fallback_used=True,
        )
        self.looked_up: list[int] = []
        self.checked: list[str] = []
        self.history_asked: list[tuple[int, int, int]] = []

    async def answered_question(self, message_id: int) -> AnsweredQuestion | None:
        self.looked_up.append(message_id)
        return self.question

    async def document_exists(self, doc_id: str) -> bool:
        self.checked.append(doc_id)
        return doc_id in INDEXED

    async def exchanges_before(
        self, session_pk: int, question_id: int, *, limit: int
    ) -> list[tuple[str, str]]:
        self.history_asked.append((session_pk, question_id, limit))
        return BEFORE


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> Stored:
    stand_in = Stored()
    for name in ("answered_question", "document_exists", "exchanges_before"):
        monkeypatch.setattr(analytics_db, name, getattr(stand_in, name))
    return stand_in


async def _promote(
    message_id: int = 812,
    *,
    expected_docs: Sequence[str] = ("services",),
    key: str | None = None,
    with_history: bool = False,
) -> EvalCase:
    return await promote.promote(
        message_id, expected_docs=expected_docs, key=key, with_history=with_history
    )


# --- the case -----------------------------------------------------------------------


async def test_a_question_becomes_a_case_that_expects_the_article_written_for_it(
    stored: Stored,
) -> None:
    case = await _promote()

    assert stored.looked_up == [812]
    assert case == EvalCase(
        key="promoted-812",
        question=QUESTION,
        category="mihail_related",
        expected_doc_ids=("services",),
        notes=(
            "Promoted from a visitor's question (message 812, asked 2026-10-12). It was "
            "routed as mihail_related and answered with the fallback."
        ),
    )
    assert case.reference_answer is None, "that is a person's to write, from the article"
    assert stored.history_asked == [], "no history unless it is asked for"


async def test_a_question_that_was_turned_away_is_promoted_as_one_that_should_be_answered(
    stored: Stored,
) -> None:
    """The case names documents that answer it, so the route it should take is the one
    that searches. A promoted misroute is a test of exactly that, and its note says
    what happened instead."""
    stored.question = AnsweredQuestion(
        question_id=812,
        question="What is Glotsmith?",
        asked_at=ASKED,
        session_pk=41,
        route="out_of_scope",
        fallback_used=False,
    )

    case = await _promote()

    assert case.category == "mihail_related"
    assert case.notes is not None
    assert case.notes.endswith("It was routed as out_of_scope.")


async def test_the_id_may_be_the_answers_and_the_case_is_named_for_the_question(
    stored: Stored,
) -> None:
    """813 is the answer to 812. The stand-in returns the question either way, as the
    query does; what is checked here is which id the key and the note carry."""
    case = await _promote(813)

    assert stored.looked_up == [813]
    assert case.key == "promoted-812"
    assert case.notes is not None
    assert "message 812" in case.notes


async def test_several_documents_are_listed_once_each_in_the_order_given(stored: Stored) -> None:
    case = await _promote(expected_docs=["faq", "services", "faq"])

    assert case.expected_doc_ids == ("faq", "services")
    assert stored.checked == ["faq", "services"]


@pytest.mark.usefixtures("stored")
async def test_a_key_of_ones_own_replaces_the_placeholder() -> None:
    assert (await _promote(key="day-rate-pricing")).key == "day-rate-pricing"


# --- the history ----------------------------------------------------------------------


async def test_with_history_the_case_carries_the_conversation_before_the_question(
    stored: Stored,
) -> None:
    case = await _promote(with_history=True)

    assert case.history == (
        Turn(role="user", content="What does he do?"),
        Turn(role="assistant", content="He is a Solutions Architect."),
        Turn(role="user", content="Does he take on consulting?"),
        Turn(role="assistant", content="Yes, he does. Would you like to know how?"),
    )
    # As many exchanges as the assistant itself had when it answered: the memory
    # window, of the conversation the question was in, before the question.
    assert stored.history_asked == [(41, 812, get_settings().memory_window_turns)]


# --- what is refused ------------------------------------------------------------------


async def test_an_id_that_names_no_answered_question_is_refused(stored: Stored) -> None:
    stored.question = None

    with pytest.raises(AnalyticsError, match="No answered question has the id 999"):
        await _promote(999)

    assert stored.checked == [], "nothing else is read once the question is not there"


async def test_a_document_that_is_not_indexed_is_refused_by_name(stored: Stored) -> None:
    """A case expecting a document nothing has indexed can only fail, and `evals run`
    would refuse the whole dataset for it. Better said now than at the next run."""
    with pytest.raises(AnalyticsError, match="No indexed article has the doc_id 'servises'"):
        await _promote(expected_docs=["services", "servises"], with_history=True)

    assert stored.history_asked == [], "the conversation is not read for a case that is refused"


@pytest.mark.parametrize("key", ["Day Rate", "day_rate", "day-rate-", "x" * 61])
async def test_a_key_the_datasets_would_refuse_is_refused_before_anything_is_read(
    stored: Stored, key: str
) -> None:
    """The rule is the dataset's own, asked of the key alone. First, so that a wrong
    key and a wrong id are found in one run and not two. The refusal gives the rule
    and none of pydantic's own message, which quotes what it was given."""
    with pytest.raises(AnalyticsError) as refused:
        await _promote(key=key)

    assert str(refused.value) == (
        "--key takes lower-case words joined by hyphens, at most 60 characters: day-rate-pricing."
    )
    assert stored.looked_up == []


async def test_a_conversation_with_an_empty_message_is_refused_in_one_line(
    stored: Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer stored as an empty string cannot be a turn of a case. That used to be
    a traceback from pydantic, raised while the history was put together. It is now
    the same one-line refusal as anything else, naming the field and saying what to
    do, and never quoting the conversation: pydantic's own text would."""

    async def with_a_blank(  # ruff: ignore[unused-async]
        session_pk: int, question_id: int, *, limit: int
    ) -> list[tuple[str, str]]:
        assert (session_pk, question_id, limit) == (41, 812, 25)
        return [("Does he take on consulting?", "")]

    monkeypatch.setattr(analytics_db, "exchanges_before", with_a_blank)

    with pytest.raises(AnalyticsError) as refused:
        await _promote(with_history=True)

    message = str(refused.value)
    assert message.startswith("That does not make a valid eval case: history.")
    assert "promote the question without --with-history" in message
    assert "Does he take on consulting?" not in message
    assert "input" not in message
    assert stored.looked_up == [812]


# --- a character YAML would fold away ---------------------------------------------------


async def test_the_one_character_yaml_would_lose_is_made_the_line_break_it_means(
    stored: Stored, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U+0085, "next line". The API lets it through in a message, the dumper writes
    it as it is, and YAML reads it back as a line break and folds it into a space:
    "line one line two". Of every character the API accepts it was the one the review
    of this stage found that did not survive the file. It is made a newline, in the
    question and in the history, and then it does."""
    next_line = chr(0x85)
    stored.question = AnsweredQuestion(
        question_id=812,
        question=f"line one{next_line}line two",
        asked_at=ASKED,
        session_pk=41,
        route="mihail_related",
        fallback_used=False,
    )

    async def earlier(  # ruff: ignore[unused-async]
        session_pk: int, question_id: int, *, limit: int
    ) -> list[tuple[str, str]]:
        assert (session_pk, question_id) == (41, 812)
        assert limit > 0
        return [(f"asked{next_line}earlier", f"answered{next_line}earlier")]

    monkeypatch.setattr(analytics_db, "exchanges_before", earlier)

    case = await _promote(with_history=True)

    assert case.question == "line one\nline two"
    assert [turn.content for turn in case.history] == ["asked\nearlier", "answered\nearlier"]
    [loaded] = _loaded(promote.as_yaml(case)).cases
    assert loaded == case


# --- what is printed --------------------------------------------------------------------


def _loaded(printed: str) -> Dataset:
    """The printed text, as the eval harness would read it from a dataset file."""
    text = f"name: {datasets.HOLDING}\ndescription: Promoted cases.\ncases:\n{printed}"
    return Dataset.model_validate(strict_yaml.load(text))


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "yes",
        "null",
        "2026",
        "rates: day or week? # and is there a discount",
        "- first of all, who is he?",
        'He said "call me", and then what\'s next?',
        "Two lines:\n  the second indented\n\nand a third after a blank one",
        "Tarifs à Zürich, s'il vous plaît",
        "a question that goes on " * 12,
        "{is this: a mapping?}",
        "*not an alias, & not an anchor",
    ],
    ids=[
        "plain",
        "a-yaml-boolean",
        "a-yaml-null",
        "a-yaml-number",
        "colon-and-hash",
        "leading-dash",
        "quotes",
        "several-lines",
        "not-ascii",
        "longer-than-a-line",
        "braces",
        "star-and-ampersand",
    ],
)
def test_what_is_printed_loads_back_as_the_case_it_was_made_from(question: str) -> None:
    """A visitor may type anything, and YAML gives meaning to a good deal of it: a
    bare ``yes`` is true, a colon starts a mapping, a hash a comment. The dumper has
    to quote all of that, and this is the proof that it does."""
    case = EvalCase(
        key="promoted-812",
        question=question,
        category="mihail_related",
        history=(
            Turn(role="user", content=question),
            Turn(role="assistant", content="An answer: with a colon, and a # too."),
        ),
        expected_doc_ids=("services", "faq"),
        notes="Promoted from a visitor's question (message 812, asked 2026-10-12).",
    )

    [loaded] = _loaded(promote.as_yaml(case)).cases

    assert loaded == case


def test_a_case_is_printed_as_one_item_of_the_list_with_no_field_it_does_not_set() -> None:
    case = EvalCase(
        key="promoted-812",
        question=QUESTION,
        category="mihail_related",
        expected_doc_ids=("services",),
    )

    assert promote.as_yaml(case) == (
        "  - key: promoted-812\n"
        f"    question: {QUESTION}\n"
        "    category: mihail_related\n"
        "    expected_doc_ids:\n"
        "    - services\n"
    )


def test_everything_the_command_prints_can_be_pasted_into_the_file_as_it_is() -> None:
    """The instructions after the case are YAML comments, so a careless paste of the
    whole output still gives a file that loads, with the one case in it."""
    case = EvalCase(
        key="promoted-812",
        question=QUESTION,
        category="mihail_related",
        expected_doc_ids=("services",),
    )

    advice = promote.what_to_do(case)
    [loaded] = _loaded(promote.as_yaml(case) + advice + "\n").cases

    assert loaded == case
    assert all(line.startswith("#") for line in advice.splitlines())


def test_the_advice_says_where_the_case_goes_and_what_to_do_first() -> None:
    plain = EvalCase(key="promoted-812", question=QUESTION, category="mihail_related")
    named = EvalCase(
        key="day-rate-pricing",
        question=QUESTION,
        category="mihail_related",
        history=(Turn(role="user", content="Hi"), Turn(role="assistant", content="Hello.")),
    )

    advice = promote.what_to_do(plain)

    assert "to go under `cases:` in datasets/promoted.yaml" in advice
    assert "The repository is public, a commit is for good" in advice
    assert "conversations are deleted after 90 days" in advice
    assert "1. Rewrite the question in your own words." in advice
    assert "anything that says who" in advice
    assert "Write its reference_answer from the article" in advice
    assert "Give it a key that says what it tests." in advice
    assert "python -m portfolio_ai.evals check datasets/promoted.yaml" in advice

    with_history = promote.what_to_do(named)
    assert "1. Rewrite the question and the history in your own words." in with_history
    assert "Check that its key says what it tests." in with_history


def test_a_letter_outside_ascii_is_printed_as_itself() -> None:
    """The case is read by a person before it is committed, and rewritten. A question
    printed as a row of escapes cannot be read."""
    case = EvalCase(
        key="promoted-812",
        question="Tarifs à Zürich ?",
        category="mihail_related",
    )

    assert "    question: Tarifs à Zürich ?\n" in promote.as_yaml(case)
