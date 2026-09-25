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
from portfolio_ai.db import evals as evals_db
from portfolio_ai.evals import runner
from portfolio_ai.evals.datasets import Dataset

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
    assert record.config["judge"] == {"model": "gpt-5", "prompt": "judge@1"}


async def test_a_dry_run_leaves_nothing_behind(fake: FakeOpenAI) -> None:
    await runner.prepare(DATASET, label="dry", config=CONFIG, judge_model="gpt-5")

    assert fake.requests == []
    assert await evals_db.list_runs() == []
    assert not await evals_db.label_taken("dry")
