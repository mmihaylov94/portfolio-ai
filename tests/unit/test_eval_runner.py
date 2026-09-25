"""The runner: every case answered, graded and stored, and what happens when one fails.

The assistant, the judge and the database are all replaced here, each with a stand-in
that records what it was asked. What is left is the runner's own logic: which cases
run, how many at once, what is stored for each, and how a run ends -- complete, with
failures recorded, or failed and marked so.

tests/integration/test_eval_run.py runs the same thing against a real database, with
only OpenAI faked.
"""

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

import pytest

from portfolio_ai.assistant import agent
from portfolio_ai.assistant.agent import (
    AssistantConfig,
    Classified,
    Done,
    Event,
    Searched,
    Token,
    TurnResult,
)
from portfolio_ai.assistant.classifier import Classification
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.db.evals import (
    Corpus,
    ResultRow,
    RunRecord,
    StoredDataset,
    StoredResult,
    SyncedDataset,
)
from portfolio_ai.evals import judge as judging
from portfolio_ai.evals import runner
from portfolio_ai.evals.datasets import Dataset
from portfolio_ai.exceptions import AssistantError, ConfigError, EvalError
from portfolio_ai.llm.responses import CallUsage

CONFIG = AssistantConfig(
    chat_model="gpt-5-mini",
    classifier_model="gpt-5-mini",
    classifier_effort=None,
    chat_effort=None,
    top_k=20,
    max_search_rounds=3,
)

DATASET = Dataset.model_validate(
    {
        "name": "golden_test",
        "description": "Three cases, one of each route.",
        "cases": [
            {
                "key": "job-title",
                "question": "What is Mihail's job title?",
                "category": "mihail_related",
                "expected_doc_ids": ["about-mihail"],
                "reference_answer": "A Solutions Architect.",
            },
            {"key": "hello", "question": "Hi there!", "category": "small_talk"},
            {"key": "weather", "question": "Weather in London?", "category": "out_of_scope"},
        ],
    }
)

CHUNK = RetrievedChunk(
    chunk_id=11,
    doc_id="about-mihail",
    title="About Mihail Mihaylov",
    url="https://mihaylov.io/",
    section="job-title",
    section_title="What is Mihail's job title?",
    content="Solutions Architect.",
    score=0.8,
)


def _usage(step: str, cost: str) -> CallUsage:
    return CallUsage(
        step=step,
        model="gpt-5-mini",
        prompt=None,
        input_tokens=500,
        cached_tokens=0,
        output_tokens=50,
        reasoning_tokens=0,
        latency_ms=100,
        cost=Decimal(cost),
    )


def _events(route: Classification, reply: str) -> list[Event]:
    chunks = [CHUNK] if route == "mihail_related" else []
    result = TurnResult(
        reply=reply,
        classification=route,
        citations=[],
        searches=[],
        retrieved_chunk_ids=[chunk.chunk_id for chunk in chunks],
        top_score=0.8 if chunks else None,
        fallback_used=False,
        links_removed=0,
        calls=[_usage("answer", "0.004")],
        model="gpt-5-mini",
        latency_ms=9000,
        first_token_ms=7000,
    )
    searched: list[Event] = [Searched("job title", chunks, 0.8)] if chunks else []
    return [Classified(route), *searched, Token(reply), Done(result)]


@dataclass
class Assistant:
    """Stands in for agent.respond: scripted events per question, or an error."""

    scripts: dict[str, list[Event] | Exception] = field(
        default_factory=lambda: {
            "What is Mihail's job title?": _events(
                "mihail_related", "He is a Solutions Architect."
            ),
            "Hi there!": _events("small_talk", "Hi, how can I help?"),
            "Weather in London?": _events("out_of_scope", "I can only help with Mihail."),
        }
    )
    # Set to hold every answer at its first event, for the cancellation test.
    hold: asyncio.Event | None = None
    running: int = 0
    most_at_once: int = 0

    async def respond(
        self,
        message: str,
        history: Sequence[HistoryMessage] = (),  # ruff: ignore[unused-method-argument]
        *,
        config: AssistantConfig | None = None,  # ruff: ignore[unused-method-argument]
    ) -> AsyncIterator[Event]:
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            await asyncio.sleep(0.01)
            if self.hold is not None:
                await self.hold.wait()
            script = self.scripts[message]
            if isinstance(script, Exception):
                raise script
            for event in script:
                yield event
        finally:
            self.running -= 1


@dataclass
class Judge:
    """Stands in for judge.judge: a fixed verdict, or an error."""

    error: Exception | None = None
    asked: list[str] = field(default_factory=list)

    async def judge(
        self,
        case: Any,
        *,
        route: str,  # ruff: ignore[unused-method-argument]
        chunks: Any,  # ruff: ignore[unused-method-argument]
        answer: str,  # ruff: ignore[unused-method-argument]
        model: str,  # ruff: ignore[unused-method-argument]
    ) -> judging.Judgement:
        await asyncio.sleep(0)
        self.asked.append(case.key)
        if self.error is not None:
            raise self.error
        verdict = judging.Verdict(
            faithfulness=5, completeness=4, style=5, declined=False, rationale="Good."
        )
        return judging.Judgement(verdict, _usage("judge", "0.02"))


@dataclass
class Store:
    """Stands in for db/evals.py: everything kept in lists."""

    taken: set[str] = field(default_factory=set)
    results: list[StoredResult] = field(default_factory=list)
    finished: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    configs: list[dict[str, object]] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)
    doc_ids: frozenset[str] = frozenset({"about-mihail", "faq"})
    # What the database holds under the dataset's name before the run.
    stored: StoredDataset | None = None

    async def label_taken(self, label: str) -> bool:
        await asyncio.sleep(0)
        return label in self.taken

    async def corpus_state(self) -> Corpus:
        await asyncio.sleep(0)
        return Corpus(documents=2, chunks=20, fingerprint="abc123abc123", doc_ids=self.doc_ids)

    async def stored_dataset(self, name: str) -> StoredDataset | None:  # ruff: ignore[unused-method-argument]
        await asyncio.sleep(0)
        return self.stored

    async def sync_dataset(self, dataset: Dataset) -> SyncedDataset:
        await asyncio.sleep(0)
        self.synced.append(dataset.name)
        ids = {case.key: number for number, case in enumerate(dataset.cases, start=1)}
        return SyncedDataset(id=1, case_ids=ids, state="created")

    # Every stand-in below takes the real function's arguments, used or not, because
    # the runner calls it exactly as it calls the real one.
    async def create_run(
        self,
        *,
        dataset_id: int,  # ruff: ignore[unused-method-argument]
        label: str,
        config: dict[str, object],
    ) -> int:
        await asyncio.sleep(0)
        self.taken.add(label)
        self.configs.append(config)
        return 7

    async def insert_result(
        self,
        run_id: int,  # ruff: ignore[unused-method-argument]
        result: StoredResult,
    ) -> None:
        await asyncio.sleep(0)
        self.results.append(result)

    async def finish_run(
        self,
        run_id: int,  # ruff: ignore[unused-method-argument]
        *,
        status: str,
        totals: dict[str, Any] | None,
    ) -> None:
        await asyncio.sleep(0)
        self.finished.append((status, totals))

    async def results_for(
        self,
        run_id: int,  # ruff: ignore[unused-method-argument]
    ) -> list[ResultRow]:
        await asyncio.sleep(0)
        cases = dict(enumerate(DATASET.cases, start=1))
        return [
            ResultRow(
                case_id=result.case_id,
                key=cases[result.case_id].key,
                question=cases[result.case_id].question,
                category=cases[result.case_id].category,
                also_accept=[],
                expected_doc_ids=list(cases[result.case_id].expected_doc_ids),
                expect_fallback=False,
                answer=result.answer,
                route=result.classification,
                retrieved_doc_ids=result.retrieved_doc_ids,
                recall=result.recall,
                precision=result.precision,
                mrr=result.mrr,
                judge_scores=result.judge_scores,
                judge_rationale=result.judge_rationale,
                violations=result.violations,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost=result.cost,
                judge_cost=result.judge_cost,
                latency_ms=result.latency_ms,
                first_token_ms=result.first_token_ms,
                top_score=result.top_score,
                fallback_used=result.fallback_used,
                calls=result.calls,
                error=result.error,
            )
            for result in self.results
        ]

    async def find_run(self, label: str) -> RunRecord:
        await asyncio.sleep(0)
        status, totals = self.finished[-1]
        return RunRecord(
            id=7,
            label=label,
            dataset_id=1,
            dataset="golden_test",
            status=status,
            config={},
            totals=totals,
            started_at=dt.datetime(2026, 9, 23, tzinfo=dt.UTC),
            finished_at=None,
        )


@pytest.fixture
def assistant(monkeypatch: pytest.MonkeyPatch) -> Assistant:
    stand_in = Assistant()
    monkeypatch.setattr(agent, "respond", stand_in.respond)
    return stand_in


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> Judge:
    stand_in = Judge()
    monkeypatch.setattr(judging, "judge", stand_in.judge)
    return stand_in


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    stand_in = Store()
    for name in (
        "label_taken",
        "corpus_state",
        "stored_dataset",
        "sync_dataset",
        "create_run",
        "insert_result",
        "finish_run",
        "find_run",
    ):
        monkeypatch.setattr(evals_db, name, getattr(stand_in, name))
    monkeypatch.setattr(evals_db, "results", stand_in.results_for)
    # The git checkout the tests happen to run in is not part of what they test.
    monkeypatch.setattr(runner, "git_revision", lambda: {"revision": "abc", "dirty": False})
    return stand_in


async def _plan(**overrides: Any) -> runner.Plan:
    arguments: dict[str, Any] = {
        "label": "baseline",
        "config": CONFIG,
        "judge_model": "gpt-5",
        "concurrency": 2,
        **overrides,
    }
    return await runner.prepare(DATASET, **arguments)


def _ignore(progress: runner.Progress) -> None:
    del progress


# --- preparing ------------------------------------------------------------------


async def test_a_label_already_used_is_refused_before_anything_runs(store: Store) -> None:
    store.taken.add("baseline")

    with pytest.raises(EvalError, match="already exists"):
        await _plan()


@pytest.mark.usefixtures("store")
async def test_only_picks_cases_and_an_unknown_one_is_refused() -> None:
    plan = await _plan(only=frozenset({"hello"}))

    assert [case.key for case in plan.cases] == ["hello"]
    assert plan.subset
    with pytest.raises(EvalError, match="no case called: nope"):
        await _plan(only=frozenset({"nope"}))


@pytest.mark.usefixtures("store")
async def test_a_model_the_price_table_does_not_know_is_refused() -> None:
    """A typo would fail every call and pay for every answer that did not."""
    with pytest.raises(EvalError, match="No price for gpt-5-mni, gtp-5"):
        await _plan(config=replace(CONFIG, chat_model="gpt-5-mni"), judge_model="gtp-5")


async def test_a_changed_dataset_with_a_complete_run_is_refused_before_anything_runs(
    store: Store,
) -> None:
    """The same freeze sync_dataset enforces, checked early so a dry run reports it."""
    store.stored = StoredDataset(content_hash="an-older-version", complete_runs=1)

    with pytest.raises(EvalError, match="frozen"):
        await _plan()


async def test_the_plan_says_where_the_dataset_stands(store: Store) -> None:
    assert (await _plan()).dataset_state.startswith("new")

    store.stored = StoredDataset(content_hash="an-older-version", complete_runs=0)
    assert (await _plan()).dataset_state.startswith("changed")

    store.stored = StoredDataset(content_hash=DATASET.content_hash, complete_runs=2)
    assert (await _plan()).dataset_state == "unchanged, frozen by 2 complete run(s)"


async def test_a_case_expecting_a_document_that_is_not_indexed_is_refused(store: Store) -> None:
    """Every such case would score as a retrieval miss that is nothing of the kind."""
    store.doc_ids = frozenset({"faq"})

    with pytest.raises(EvalError, match="not indexed: about-mihail"):
        await _plan()


# --- running --------------------------------------------------------------------


@pytest.mark.usefixtures("assistant")
async def test_every_case_is_answered_measured_graded_and_stored(
    judge: Judge, store: Store
) -> None:
    seen: list[str] = []

    record = await runner.execute(await _plan(), on_result=lambda done: seen.append(done.key))

    assert sorted(seen) == ["hello", "job-title", "weather"]
    assert store.synced == ["golden_test"], "the dataset is stored before the run starts"
    stored = {result.case_id: result for result in store.results}
    job_title = stored[1]
    assert job_title.retrieved_doc_ids == ["about-mihail"]
    assert (job_title.recall, job_title.mrr) == (1.0, 1.0)
    assert job_title.judge_scores == {
        "faithfulness": 5,
        "completeness": 4,
        "style": 5,
        "declined": False,
    }
    assert job_title.cost == Decimal("0.004")
    assert job_title.judge_cost == Decimal("0.02")
    assert [call["step"] for call in job_title.calls] == ["answer", "judge"], "every call, kept"
    assert stored[2].recall is None, "small talk expects no documents"
    assert sorted(judge.asked) == ["hello", "job-title"], (
        "the fixed out-of-scope reply is not graded"
    )
    assert record.status == "complete"
    assert record.totals is not None
    assert record.totals["cases"] == 3


@pytest.mark.usefixtures("assistant", "judge")
async def test_the_run_records_the_configuration_that_produced_it(store: Store) -> None:
    await runner.execute(await _plan(), on_result=_ignore)

    [config] = store.configs
    assert config["chat_model"] == "gpt-5-mini"
    assert config["judge"] == {"model": "gpt-5", "prompt": "judge@1"}
    assert config["corpus"] == {"documents": 2, "chunks": 20, "fingerprint": "abc123abc123"}
    assert config["git"] == {"revision": "abc", "dirty": False}
    assert "judge@1" in str(config["prompts"])


@pytest.mark.usefixtures("judge")
async def test_a_case_that_fails_is_recorded_and_the_rest_still_run(
    assistant: Assistant, store: Store
) -> None:
    assistant.scripts["Hi there!"] = AssistantError("OpenAI call failed (APIConnectionError)")

    record = await runner.execute(await _plan(), on_result=_ignore)

    failed = next(result for result in store.results if result.case_id == 2)
    assert failed.answer is None
    assert failed.error == "answer: AssistantError: OpenAI call failed (APIConnectionError)"
    assert record.status == "complete"
    assert record.totals is not None
    assert record.totals["errors"] == {"answer": 1, "judge": 0}


@pytest.mark.usefixtures("assistant")
async def test_a_judge_that_fails_leaves_the_answer_and_says_why(
    judge: Judge, store: Store
) -> None:
    judge.error = AssistantError("OpenAI call failed (RateLimitError)")

    await runner.execute(await _plan(), on_result=_ignore)

    job_title = next(result for result in store.results if result.case_id == 1)
    assert job_title.answer == "He is a Solutions Architect."
    assert job_title.judge_scores is None
    assert job_title.error == "judge: AssistantError: OpenAI call failed (RateLimitError)"


@pytest.mark.usefixtures("judge")
async def test_a_rejected_key_stops_the_run_and_marks_it_failed(
    assistant: Assistant, store: Store
) -> None:
    """Every case would fail the same way, so none of the rest is worth paying for."""
    assistant.scripts["Hi there!"] = ConfigError("OpenAI rejected the API key.")

    with pytest.raises(ConfigError, match="rejected the API key"):
        await runner.execute(await _plan(), on_result=_ignore)

    assert store.finished == [("failed", None)]


@pytest.mark.usefixtures("judge", "store")
async def test_no_more_cases_run_at_once_than_asked(assistant: Assistant) -> None:
    await runner.execute(await _plan(concurrency=2), on_result=_ignore)

    assert assistant.most_at_once == 2


@pytest.mark.usefixtures("judge")
async def test_a_run_interrupted_part_way_is_marked_failed(
    assistant: Assistant, store: Store
) -> None:
    """Ctrl-C reaches the run as a cancellation; the run must not stay "running"."""
    assistant.hold = asyncio.Event()
    running = asyncio.create_task(runner.execute(await _plan(), on_result=_ignore))
    async with asyncio.timeout(5):
        while assistant.running == 0:  # ruff: ignore[async-busy-wait]
            await asyncio.sleep(0)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert store.finished == [("failed", None)]
