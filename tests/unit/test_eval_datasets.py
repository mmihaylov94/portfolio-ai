"""Golden datasets: what a valid file is, and what freezes one.

The last test here runs in CI on the real golden_v1.yaml, so a dataset edit that no
longer validates fails the build rather than the next eval run.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field, ValidationError

from portfolio_ai.assistant.prompts.loader import RAG_AGENT, SMALL_TALK
from portfolio_ai.evals import datasets
from portfolio_ai.evals.datasets import Dataset, EvalCase
from portfolio_ai.exceptions import EvalError

GOLDEN = Path(__file__).resolve().parents[2] / "datasets" / "golden_v1.yaml"

# Mihail's two published addresses: the hiring one printed on his CV, and the one for
# projects and general inquiries. The site lists both. Public by his choice, and what
# the contact cases check answers for.
PUBLISHED_ADDRESSES = {"m.mihaylov94@gmail.com", "mihaylov.dev@gmail.com"}


def _case(**overrides: Any) -> dict[str, Any]:
    return {
        "key": "job-title",
        "question": "What is Mihail's job title?",
        "category": "mihail_related",
        "expected_doc_ids": ["about-mihail"],
        **overrides,
    }


def _dataset(*cases: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {"name": "golden_test", "description": "Test cases.", "cases": list(cases), **overrides}


def test_the_golden_dataset_is_valid() -> None:
    dataset = datasets.load(GOLDEN)

    assert dataset.name == "golden_v1"
    assert len(dataset.cases) >= 50
    assert {case.category for case in dataset.cases} == {
        "mihail_related",
        "small_talk",
        "out_of_scope",
    }


def test_the_injection_case_would_catch_either_answering_prompt_leaking() -> None:
    """Its phrases have to come from the prompt of every route that writes an answer.
    Taken from rag_agent.md alone, a leak through small talk scored clean."""
    [case] = [case for case in datasets.load(GOLDEN).cases if case.key == "prompt-injection"]

    for prompt in (RAG_AGENT, SMALL_TALK):
        text = prompt.text.lower()
        assert any(phrase.lower() in text for phrase in case.must_not_include), prompt.name


@pytest.mark.parametrize("path", sorted(GOLDEN.parent.glob("*.yaml")), ids=lambda path: path.name)
def test_no_dataset_holds_an_email_address_but_mihails_published_ones(path: Path) -> None:
    """The repository is public. Every dataset, not only golden_v1, because a case promoted
    from real traffic could carry a visitor's address into a public commit."""
    found = set(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", path.read_text(encoding="utf-8")))

    assert found <= PUBLISHED_ADDRESSES, sorted(found - PUBLISHED_ADDRESSES)


@pytest.mark.parametrize(
    ("case", "problem"),
    [
        (_case(category="small_talk"), "only mihail_related questions search"),
        (_case(expect_fallback=True), "expects no documents"),
        (
            _case(category="small_talk", expected_doc_ids=[], expect_fallback=True),
            "only a mihail_related question",
        ),
        (_case(also_accept=["mihail_related"]), "repeats the category"),
        (_case(history=[{"role": "user", "content": "Hi"}]), "ends with an answer"),
        (
            _case(
                history=[
                    {"role": "assistant", "content": "Hello."},
                    {"role": "user", "content": "Hi"},
                ]
            ),
            "alternates",
        ),
        (_case(expected_docs=["about-mihail"]), "Extra inputs are not permitted"),
        (_case(category="banter"), "Input should be"),
        (_case(key="Job Title"), "String should match pattern"),
    ],
    ids=[
        "documents-without-search",
        "fallback-with-documents",
        "fallback-outside-search",
        "also-accept-repeats",
        "history-without-answer",
        "history-starting-with-answer",
        "misspelt-field",
        "unknown-category",
        "key-with-spaces",
    ],
)
def test_an_inconsistent_case_is_refused(case: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        Dataset.model_validate(_dataset(case))


def test_keys_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="repeated: job-title"):
        Dataset.model_validate(_dataset(_case(), _case(question="And his title?")))


def test_the_hash_ignores_field_order_and_the_description() -> None:
    first = Dataset.model_validate(_dataset(_case()))
    reordered = Dataset.model_validate(
        _dataset(dict(reversed(list(_case().items()))), description="Reworded.")
    )

    assert first.content_hash == reordered.content_hash


def test_the_hash_changes_when_a_case_does() -> None:
    first = Dataset.model_validate(_dataset(_case()))
    edited = Dataset.model_validate(_dataset(_case(question="What is his role?")))

    assert first.content_hash != edited.content_hash


def test_a_field_added_to_the_model_later_does_not_change_the_hash() -> None:
    """Step 7 will give cases a source_message_id. If that changed the hash, every
    dataset frozen by then would read as edited, and be refused."""

    class LaterCase(EvalCase):
        source_message_id: int | None = None

    class LaterDataset(Dataset):
        cases: tuple[LaterCase, ...] = Field(min_length=1)

    raw = _dataset(_case())

    assert LaterDataset.model_validate(raw).content_hash == Dataset.model_validate(raw).content_hash


def test_writing_out_a_default_does_not_change_the_hash() -> None:
    explicit = _case(expect_fallback=False, history=[], must_include=[])

    assert (
        Dataset.model_validate(_dataset(explicit)).content_hash
        == Dataset.model_validate(_dataset(_case())).content_hash
    )


def test_a_key_written_twice_is_refused(tmp_path: Path) -> None:
    """Plain YAML would keep the second without a word, and score on it."""
    path = tmp_path / "golden_v1.yaml"
    path.write_text(
        "name: golden_v1\ndescription: x\ncases:\n"
        "  - key: a\n    question: q\n    category: small_talk\n    category: mihail_related\n",
        encoding="utf-8",
    )

    with pytest.raises(EvalError, match="'category' appears twice"):
        datasets.load(path)


def test_a_file_must_carry_its_own_name(tmp_path: Path) -> None:
    path = tmp_path / "golden_v9.yaml"
    path.write_text(
        "name: golden_v1\ndescription: x\ncases:\n"
        "  - {key: a, question: q, category: small_talk}\n",
        encoding="utf-8",
    )

    with pytest.raises(EvalError, match="must match the file"):
        datasets.load(path)


def test_a_file_that_is_not_yaml_is_refused_with_its_name(tmp_path: Path) -> None:
    path = tmp_path / "golden_v1.yaml"
    path.write_text("cases: [unclosed", encoding="utf-8")

    with pytest.raises(EvalError, match="not valid YAML"):
        datasets.load(path)


def test_a_missing_file_is_refused_with_its_name(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="Cannot read"):
        datasets.load(tmp_path / "golden_v1.yaml")


def test_a_name_means_the_file_in_datasets_and_a_path_means_itself() -> None:
    assert datasets.path_for("golden_v1") == Path("datasets/golden_v1.yaml")
    assert datasets.path_for("other/place.yaml") == Path("other/place.yaml")


def test_history_is_handed_to_the_assistant_as_it_expects() -> None:
    case = Dataset.model_validate(
        _dataset(
            _case(
                history=[
                    {"role": "user", "content": "What is Glotsmith?"},
                    {"role": "assistant", "content": "A language notebook."},
                ]
            )
        )
    ).cases[0]

    messages = case.history_messages()

    assert [(message.role, message.content) for message in messages] == [
        ("user", "What is Glotsmith?"),
        ("assistant", "A language notebook."),
    ]
    assert case.accepted_routes == frozenset({"mihail_related"})
