"""A whole eval run: the real assistant, the real judge code, the real database.

Only OpenAI is fake. Three cases, one for each route, run one at a time so the
scripted replies arrive in a known order: classify, search, answer and grade the
first; classify, answer and grade the second; classify the third, whose fixed reply
is not graded.
"""

import corpus
import pytest
from openai_fake import FakeOpenAI, install_fake_openai

from portfolio_ai.assistant.agent import AssistantConfig
from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.db import evals as evals_db
from portfolio_ai.evals import datasets, runner
from portfolio_ai.evals.datasets import Dataset
from portfolio_ai.exceptions import EvalError

pytestmark = pytest.mark.integration

DATASET = Dataset.model_validate(
    {
        "name": "golden_test",
        "description": "One case per route.",
        "cases": [
            {
                "key": "laravel",
                "question": "Does Mihail work with Laravel?",
                "category": "mihail_related",
                "expected_doc_ids": ["tech-stack"],
                "reference_answer": "Yes, it is his main PHP framework.",
                "must_include": ["Laravel"],
            },
            {"key": "hello", "question": "Hi there!", "category": "small_talk"},
            {"key": "weather", "question": "Weather in London?", "category": "out_of_scope"},
        ],
    }
)

CONFIG = AssistantConfig(
    chat_model="gpt-5-mini",
    classifier_model="gpt-5-mini",
    classifier_effort=None,
    chat_effort=None,
    top_k=5,
    max_search_rounds=3,
)


@pytest.fixture(autouse=True)
async def _knowledge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "git_revision", lambda: {"revision": "test", "dirty": False})
    await corpus.clear()
    await corpus.seed(
        "tech-stack",
        "Technical Skills and Tools",
        [("Does Mihail work with Laravel?", "Yes, it is his main PHP framework.", corpus.axis(0))],
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


async def test_a_run_answers_grades_and_stores_every_case(fake: FakeOpenAI) -> None:
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")
    fake.judge(faithfulness=5, completeness=5, style=4, rationale="Right, and brief.")
    fake.classify("small_talk")
    fake.say("Hi, how can I help?")
    fake.judge(faithfulness=None, completeness=None, style=5)
    fake.classify("out_of_scope")
    progress: list[runner.Progress] = []

    plan = await runner.prepare(
        DATASET, label="integration", config=CONFIG, judge_model="gpt-5", concurrency=1
    )
    record = await runner.execute(plan, on_result=progress.append)

    assert fake.pending == 0, "every scripted reply was used, and nothing more was asked"
    # Sorted: one case is answered at a time, but a result is stored after the next
    # case has started, so a quick one can be reported before a slow one.
    assert sorted(step.key for step in progress) == ["hello", "laravel", "weather"]

    rows = {row.key: row for row in await evals_db.results(record.id)}
    laravel = rows["laravel"]
    assert laravel.answer == "Yes, Laravel is his main PHP framework."
    assert laravel.retrieved_doc_ids == ["tech-stack"]
    assert (laravel.recall, laravel.mrr) == (1.0, 1.0)
    assert laravel.judge_scores == {
        "faithfulness": 5,
        "completeness": 5,
        "style": 4,
        "declined": False,
    }
    assert laravel.violations == {}
    assert laravel.cost is not None
    assert laravel.cost > 0
    assert laravel.judge_cost is not None
    assert laravel.judge_cost > 0
    assert rows["hello"].judge_scores is not None
    assert rows["weather"].route == "out_of_scope"
    assert rows["weather"].judge_scores is None, "the fixed reply is not graded"

    assert record.status == "complete"
    assert record.totals is not None
    assert record.totals["classification"]["accuracy"] == pytest.approx(1.0)
    assert record.totals["retrieval"]["hit_rate"] == pytest.approx(1.0)
    assert record.config["corpus"]["documents"] == 1
    assert record.config["judge"] == {"model": "gpt-5", "prompt": JUDGE.ref}


def _ignore(step: runner.Progress) -> None:
    """Progress nobody is watching."""


async def _first_run(fake: FakeOpenAI) -> None:
    """The run above, stored under the label "first" and graded by gpt-5."""
    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Laravel is his main PHP framework.")
    fake.judge(faithfulness=5, completeness=5, style=4, rationale="Right, and brief.")
    fake.classify("small_talk")
    fake.say("Hi, how can I help?")
    fake.judge(faithfulness=None, completeness=None, style=5)
    fake.classify("out_of_scope")
    plan = await runner.prepare(
        DATASET, label="first", config=CONFIG, judge_model="gpt-5", concurrency=1
    )
    await runner.execute(plan, on_result=_ignore)


@pytest.fixture
def _dataset_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-grade reads the cases back from the file the first run names. There is no
    such file for the three cases above, so loading it returns them."""
    monkeypatch.setattr(datasets, "load", lambda path: DATASET)  # ruff: ignore[unused-lambda-argument]


@pytest.mark.usefixtures("_dataset_file")
async def test_a_stored_run_is_graded_again_without_asking_anything_again(
    fake: FakeOpenAI,
) -> None:
    """`evals rejudge` against Postgres: the answers read back as they were stored, the
    passages read back by the ids stored with them, and only the judge called."""
    await _first_run(fake)
    asked = len(fake.requests)
    fake.judge(faithfulness=3, completeness=5, style=4, rationale="One claim is unsupported.")
    fake.judge(faithfulness=None, completeness=None, style=4)

    plan = await runner.prepare_rejudge(
        "first", label="second", judge_model="gpt-6.1-sol", concurrency=1
    )
    record = await runner.rejudge(plan, on_result=_ignore)

    assert fake.pending == 0
    # Two calls, both the judge's: the fixed out-of-scope reply is not graded, and
    # no question was classified, searched for or answered a second time.
    grading = fake.requests[asked:]
    assert len(grading) == 2
    assert all(request["model"] == "gpt-6.1-sol" for request in grading)
    # The judge was shown the passage the answer was written from, found by its id.
    assert "Yes, it is his main PHP framework." in str(grading[0])
    assert fake.embedded == ["Laravel PHP experience"], "nothing was searched for again"

    first = {row.key: row for row in await evals_db.results((await _run("first")).id)}
    second = {row.key: row for row in await evals_db.results(record.id)}
    assert second.keys() == first.keys()
    for key, row in second.items():
        before = first[key]
        assert (row.answer, row.route, row.retrieved_doc_ids, row.recall, row.cost) == (
            before.answer,
            before.route,
            before.retrieved_doc_ids,
            before.recall,
            before.cost,
        )
    assert second["laravel"].judge_scores == {
        "faithfulness": 3,
        "completeness": 5,
        "style": 4,
        "declined": False,
    }
    assert second["laravel"].judge_rationale == "One claim is unsupported."
    assert second["weather"].judge_scores is None

    assert record.status == "complete"
    assert record.config["judge"] == {"model": "gpt-6.1-sol", "prompt": JUDGE.ref}
    assert record.config["rejudged_from"] == "first"
    assert record.config["chat_model"] == "gpt-5-mini"
    assert record.totals is not None
    assert record.totals["judge"]["faithfulness"] == pytest.approx(3.0)


@pytest.mark.usefixtures("_dataset_file")
async def test_a_knowledge_base_indexed_again_is_refused_before_a_run_exists(
    fake: FakeOpenAI,
) -> None:
    """The same document, with the same text, indexed again: what `ingestion --force`
    does. The fingerprint is what it was and every passage has a new id, so the ids the
    first run stored point at nothing. Refused while there is still no second run."""
    await _first_run(fake)
    fingerprint = (await evals_db.corpus_state()).fingerprint
    await corpus.seed(
        "tech-stack",
        "Technical Skills and Tools",
        [("Does Mihail work with Laravel?", "Yes, it is his main PHP framework.", corpus.axis(0))],
    )
    assert (await evals_db.corpus_state()).fingerprint == fingerprint

    with pytest.raises(EvalError, match="no longer stored under the ids it recorded"):
        await runner.prepare_rejudge("first", label="second", judge_model="gpt-6.1-sol")

    assert fake.pending == 0
    assert not await evals_db.label_taken("second")


async def _run(label: str) -> evals_db.RunRecord:
    record = await evals_db.find_run(label)
    assert record is not None
    return record


async def test_a_dry_run_leaves_nothing_behind(fake: FakeOpenAI) -> None:
    await runner.prepare(DATASET, label="dry", config=CONFIG, judge_model="gpt-5")

    assert fake.requests == []
    assert await evals_db.list_runs() == []
    assert not await evals_db.label_taken("dry")
