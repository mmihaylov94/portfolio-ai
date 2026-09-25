"""The eval tables against Postgres: storing a dataset, freezing it, and a run's lifecycle.

The rule these pin down is the one the harness depends on to mean anything: a dataset
with a complete run cannot change under the same name, so every score stored against
it was earned on the same cases.
"""

from decimal import Decimal
from typing import Any

import corpus
import pytest

from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import StoredResult
from portfolio_ai.evals.datasets import Dataset
from portfolio_ai.exceptions import EvalError

pytestmark = pytest.mark.integration


def _dataset(question: str = "What is Mihail's job title?", **overrides: Any) -> Dataset:
    return Dataset.model_validate(
        {
            "name": "golden_test",
            "description": "Two cases.",
            "cases": [
                {
                    "key": "job-title",
                    "question": question,
                    "category": "mihail_related",
                    "expected_doc_ids": ["about-mihail"],
                    "must_include": ["Solutions Architect"],
                },
                {
                    "key": "are-you-mihail",
                    "question": "Are you Mihail?",
                    "category": "small_talk",
                    "also_accept": ["mihail_related"],
                    "history": [
                        {"role": "user", "content": "Hi"},
                        {"role": "assistant", "content": "Hi, how can I help?"},
                    ],
                },
            ],
            **overrides,
        }
    )


def _result(case_id: int, **overrides: Any) -> StoredResult:
    values: dict[str, Any] = {
        "case_id": case_id,
        "answer": "He is a Solutions Architect.",
        "classification": "mihail_related",
        "retrieved_chunk_ids": [1, 2],
        "retrieved_doc_ids": ["about-mihail", "faq"],
        "recall": 1.0,
        "precision": 0.5,
        "mrr": 1.0,
        "judge_scores": {"faithfulness": 5, "completeness": 4, "style": 5, "declined": False},
        "judge_rationale": "Accurate.",
        "violations": {"too_long": 7},
        "prompt_tokens": 1200,
        "completion_tokens": 90,
        "cost": Decimal("0.003812"),
        "judge_cost": Decimal("0.021"),
        "latency_ms": 9100,
        "first_token_ms": 7300,
        "top_score": 0.71,
        "fallback_used": False,
        "links_removed": 0,
        "searches": [{"name": "search_knowledgebase", "query": "job title", "chunk_ids": [1, 2]}],
        "calls": [
            {
                "step": "answer",
                "model": "gpt-5-mini",
                "reasoning_tokens": 448,
                "cost_usd": "0.0038",
            },
            {"step": "judge", "model": "gpt-5", "reasoning_tokens": 1216, "cost_usd": "0.021"},
        ],
        "error": None,
        **overrides,
    }
    return StoredResult(**values)


@pytest.fixture(autouse=True)
async def _empty() -> None:
    await corpus.clear()


async def test_a_new_dataset_is_stored_with_its_cases() -> None:
    synced = await evals_db.sync_dataset(_dataset())

    assert synced.state == "created"
    assert sorted(synced.case_ids) == ["are-you-mihail", "job-title"]


async def test_the_same_file_again_changes_nothing() -> None:
    first = await evals_db.sync_dataset(_dataset())
    second = await evals_db.sync_dataset(_dataset(description="Reworded, which is not a change."))

    assert second.state == "unchanged"
    assert second.case_ids == first.case_ids


async def test_a_changed_dataset_is_replaced_until_it_has_a_run() -> None:
    first = await evals_db.sync_dataset(_dataset())
    replaced = await evals_db.sync_dataset(_dataset("What does Mihail do for a living?"))

    assert replaced.state == "replaced"
    assert replaced.id == first.id
    assert replaced.case_ids != first.case_ids, "the cases were stored afresh"


async def test_a_dataset_with_a_complete_run_is_frozen() -> None:
    synced = await evals_db.sync_dataset(_dataset())
    run_id = await evals_db.create_run(dataset_id=synced.id, label="baseline", config={})
    await evals_db.finish_run(run_id, status="complete", totals={})

    with pytest.raises(EvalError, match="frozen"):
        await evals_db.sync_dataset(_dataset("What does Mihail do for a living?"))

    unchanged = await evals_db.sync_dataset(_dataset())
    assert unchanged.state == "unchanged", "the stored cases are the original ones"


async def test_a_run_that_failed_does_not_freeze_its_dataset() -> None:
    """A run stopped part-way has no scores worth protecting; fixing the file must work."""
    synced = await evals_db.sync_dataset(_dataset())
    run_id = await evals_db.create_run(dataset_id=synced.id, label="baseline", config={})
    await evals_db.insert_result(run_id, _result(synced.case_ids["job-title"]))
    await evals_db.finish_run(run_id, status="failed", totals=None)

    replaced = await evals_db.sync_dataset(_dataset("What does Mihail do for a living?"))

    assert replaced.state == "replaced"
    assert await evals_db.results(run_id) == [], "its partial results went with the old cases"


async def test_what_is_stored_can_be_read_without_changing_it() -> None:
    """What the dry run reports: the stored hash, and how many complete runs freeze it."""
    assert await evals_db.stored_dataset("golden_test") is None

    synced = await evals_db.sync_dataset(_dataset())
    for label, status in (("a", "complete"), ("b", "failed"), ("c", "complete")):
        run_id = await evals_db.create_run(dataset_id=synced.id, label=label, config={})
        await evals_db.finish_run(run_id, status=status, totals=None)

    stored = await evals_db.stored_dataset("golden_test")

    assert stored is not None
    assert stored.content_hash == _dataset().content_hash
    assert stored.complete_runs == 2


async def test_a_label_names_one_run() -> None:
    synced = await evals_db.sync_dataset(_dataset())
    await evals_db.create_run(dataset_id=synced.id, label="baseline", config={})

    assert await evals_db.label_taken("baseline")
    assert not await evals_db.label_taken("effort-low")
    with pytest.raises(EvalError, match="already exists"):
        await evals_db.create_run(dataset_id=synced.id, label="baseline", config={})


async def test_a_run_and_its_results_come_back_as_they_were_stored() -> None:
    synced = await evals_db.sync_dataset(_dataset())
    run_id = await evals_db.create_run(
        dataset_id=synced.id, label="baseline", config={"chat_model": "gpt-5-mini"}
    )
    job_title, are_you = synced.case_ids["job-title"], synced.case_ids["are-you-mihail"]
    await evals_db.insert_result(run_id, _result(job_title))
    await evals_db.insert_result(
        run_id,
        _result(
            are_you,
            answer=None,
            classification=None,
            recall=None,
            precision=None,
            mrr=None,
            judge_scores=None,
            cost=None,
            error="answer: AssistantError: scripted",
        ),
    )
    await evals_db.finish_run(run_id, status="complete", totals={"cases": 2})

    rows = await evals_db.results(run_id)
    record = await evals_db.find_run("baseline")

    assert [row.key for row in rows] == ["job-title", "are-you-mihail"]
    first = rows[0]
    assert first.route == "mihail_related"
    assert first.retrieved_doc_ids == ["about-mihail", "faq"]
    assert first.judge_scores == {
        "faithfulness": 5,
        "completeness": 4,
        "style": 5,
        "declined": False,
    }
    assert first.violations == {"too_long": 7}
    assert first.cost == Decimal("0.003812")
    assert first.judge_cost == Decimal("0.021000")
    assert first.first_token_ms == 7300
    assert [(call["step"], call["reasoning_tokens"]) for call in first.calls] == [
        ("answer", 448),
        ("judge", 1216),
    ]
    assert rows[1].also_accept == ["mihail_related"]
    assert rows[1].error == "answer: AssistantError: scripted"
    assert record is not None
    assert (record.status, record.totals, record.dataset) == (
        "complete",
        {"cases": 2},
        "golden_test",
    )
    assert record.config == {"chat_model": "gpt-5-mini"}
    assert record.finished_at is not None
    assert [run.label for run in await evals_db.list_runs()] == ["baseline"]


async def test_the_corpus_is_counted_and_fingerprinted() -> None:
    await corpus.seed("faq", "FAQ", [("Q", "A", corpus.axis(0)), ("Q2", "A2", corpus.axis(1))])
    before = await evals_db.corpus_state()
    await corpus.seed("hiring", "Hiring", [("Q", "A", corpus.axis(2))])
    after = await evals_db.corpus_state()

    assert (before.documents, before.chunks, before.doc_ids) == (1, 2, frozenset({"faq"}))
    assert (after.documents, after.chunks) == (2, 3)
    assert before.fingerprint != after.fingerprint
    assert len(before.fingerprint) == 12
