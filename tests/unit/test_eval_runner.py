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
from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.db import documents as documents_db
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
from portfolio_ai.evals import datasets, runner
from portfolio_ai.evals import judge as judging
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
    # What the judge says of every answer: whether it declined to answer.
    declined: bool = False
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
            faithfulness=5, completeness=4, style=5, declined=self.declined, rationale="Good."
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

    async def find_run(self, label: str) -> RunRecord | None:
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
@pytest.mark.parametrize(
    "pair",
    [
        {"chat_model": "gpt-6-luna", "chat_effort": "minimal"},
        {"classifier_model": "gpt-6-luna", "classifier_effort": "minimal"},
    ],
    ids=["chat", "classifier"],
)
async def test_an_effort_the_model_does_not_take_is_refused(pair: dict[str, Any]) -> None:
    """--chat-model without --chat-effort, or the reverse: OpenAI would refuse every call.
    The classifier's pair is a separate mistake to make, and is checked separately."""
    with pytest.raises(EvalError, match="gpt-6-luna does not take the effort 'minimal'"):
        await _plan(config=replace(CONFIG, **pair))


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
    assert config["judge"] == {"model": "gpt-5", "prompt": JUDGE.ref}
    assert config["corpus"] == {"documents": 2, "chunks": 20, "fingerprint": "abc123abc123"}
    assert config["git"] == {"revision": "abc", "dirty": False}
    assert JUDGE.ref in str(config["prompts"])


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


# --- grading a stored run again -------------------------------------------------

OLD_SCORES: dict[str, object] = {
    "faithfulness": 5,
    "completeness": 5,
    "style": 3,
    "declined": True,
}


def _held(case_id: int, route: Classification, answer: str, **overrides: Any) -> StoredResult:
    """A result as the first run stored it, graded by the first judge."""
    graded = route != "out_of_scope"
    values: dict[str, Any] = {
        "case_id": case_id,
        "answer": answer,
        "classification": route,
        "retrieved_chunk_ids": [CHUNK.chunk_id] if route == "mihail_related" else [],
        "retrieved_doc_ids": ["about-mihail"] if route == "mihail_related" else [],
        "recall": 1.0 if route == "mihail_related" else None,
        "precision": 1.0 if route == "mihail_related" else None,
        "mrr": 1.0 if route == "mihail_related" else None,
        "judge_scores": dict(OLD_SCORES) if graded else None,
        "judge_rationale": "The first judge's reading." if graded else None,
        "violations": {},
        "prompt_tokens": 500,
        "completion_tokens": 50,
        "cost": Decimal("0.004"),
        "judge_cost": Decimal("0.01") if graded else None,
        "latency_ms": 9000,
        "first_token_ms": 7000,
        "top_score": 0.8 if route == "mihail_related" else None,
        "fallback_used": False,
        "links_removed": 0,
        "searches": [],
        "calls": [
            _usage("answer", "0.004").as_record(),
            *([_usage("judge", "0.01").as_record()] if graded else []),
        ],
        "error": None,
        **overrides,
    }
    return StoredResult(**values)


def _source_config() -> dict[str, Any]:
    return {
        "chat_model": "gpt-5-mini",
        "prompts": ["classifier@2", "rag_agent@1", "judge@1"],
        "judge": {"model": "gpt-5", "prompt": "judge@1"},
        "corpus": {"documents": 2, "chunks": 20, "fingerprint": "abc123abc123"},
        "dataset": {
            "name": "golden_test",
            "content_hash": DATASET.content_hash,
            "cases": 3,
            "only": None,
        },
        "concurrency": 4,
        "git": {"revision": "old", "dirty": False},
    }


@dataclass
class Graded(Store):
    """A database that already holds one complete run, "baseline", graded by gpt-5."""

    status: str = "complete"
    source_config: dict[str, Any] = field(default_factory=_source_config)
    held: list[tuple[str, StoredResult]] = field(
        default_factory=lambda: [
            # The first judge said this one declined, which made it a finding, and the
            # rules found a bullet list in it. Only the first rests on the judge.
            (
                "job-title",
                _held(
                    1,
                    "mihail_related",
                    "He is a Solutions Architect.",
                    violations={"bullet_list": 3, "declined_answerable": True},
                ),
            ),
            ("hello", _held(2, "small_talk", "Hi, how can I help?")),
            ("weather", _held(3, "out_of_scope", "I can only help with Mihail.")),
        ]
    )
    # The passages the knowledge base still holds, by id.
    passages: dict[int, RetrievedChunk] = field(default_factory=lambda: {CHUNK.chunk_id: CHUNK})

    def __post_init__(self) -> None:
        self.taken.add("baseline")

    async def find_run(self, label: str) -> RunRecord | None:
        if label != "baseline":
            return await super().find_run(label) if self.finished else None
        await asyncio.sleep(0)
        return RunRecord(
            id=3,
            label="baseline",
            dataset_id=1,
            dataset="golden_test",
            status=self.status,
            config=self.source_config,
            totals={},
            started_at=dt.datetime(2026, 10, 9, tzinfo=dt.UTC),
            finished_at=None,
        )

    async def stored_results(
        self,
        run_id: int,  # ruff: ignore[unused-method-argument]
    ) -> list[tuple[str, StoredResult]]:
        await asyncio.sleep(0)
        return list(self.held)

    async def chunks_by_id(self, chunk_ids: Sequence[int]) -> list[RetrievedChunk]:
        await asyncio.sleep(0)
        return [self.passages[chunk_id] for chunk_id in chunk_ids if chunk_id in self.passages]


@pytest.fixture
def graded(monkeypatch: pytest.MonkeyPatch) -> Graded:
    stand_in = Graded()
    for name in (
        "label_taken",
        "corpus_state",
        "create_run",
        "insert_result",
        "finish_run",
        "find_run",
        "stored_results",
    ):
        monkeypatch.setattr(evals_db, name, getattr(stand_in, name))
    monkeypatch.setattr(evals_db, "results", stand_in.results_for)
    monkeypatch.setattr(documents_db, "chunks_by_id", stand_in.chunks_by_id)
    monkeypatch.setattr(runner, "git_revision", lambda: {"revision": "abc", "dirty": False})
    # The dataset file the source run names: here, the three cases above.
    monkeypatch.setattr(datasets, "load", lambda path: DATASET)  # ruff: ignore[unused-lambda-argument]
    return stand_in


async def _regrade(**overrides: Any) -> runner.Regrade:
    arguments: dict[str, Any] = {
        "label": "baseline-sol",
        "judge_model": "gpt-6.1-sol",
        "concurrency": 2,
        **overrides,
    }
    return await runner.prepare_rejudge("baseline", **arguments)


async def test_a_stored_run_is_graded_again_and_its_answers_are_kept(
    graded: Graded, judge: Judge
) -> None:
    record = await runner.rejudge(await _regrade(), on_result=_ignore)

    assert record.status == "complete"
    # The fixed out-of-scope reply has nothing in it to grade, the second time either.
    assert sorted(judge.asked) == ["hello", "job-title"]

    by_case = {result.case_id: result for result in graded.results}
    job_title, before = by_case[1], graded.held[0][1]
    # The answer, and everything measured about it, is the first run's.
    assert (job_title.answer, job_title.cost, job_title.latency_ms) == (
        before.answer,
        before.cost,
        before.latency_ms,
    )
    assert job_title.retrieved_chunk_ids == before.retrieved_chunk_ids
    # The judge's part is the new judge's.
    assert job_title.judge_scores == {
        "faithfulness": 5,
        "completeness": 4,
        "style": 5,
        "declined": False,
    }
    assert job_title.judge_rationale == "Good."
    assert job_title.judge_cost == Decimal("0.02")
    assert [(call["step"], call["cost_usd"]) for call in job_title.calls] == [
        ("answer", "0.004"),
        ("judge", "0.02"),
    ]
    assert by_case[3] == graded.held[2][1]


@pytest.mark.usefixtures("judge")
async def test_what_rested_on_the_first_judge_is_worked_out_again(graded: Graded) -> None:
    """The first judge read the answer as declining, which made it a finding. The new one
    does not, so the finding goes. The bullet list is in the text, and stays."""
    await runner.rejudge(await _regrade(), on_result=_ignore)

    job_title = next(result for result in graded.results if result.case_id == 1)
    assert job_title.violations == {"bullet_list": 3}


async def test_a_new_judge_that_reads_an_answer_as_declining_makes_it_a_finding(
    graded: Graded, judge: Judge
) -> None:
    graded.held[0] = ("job-title", _held(1, "mihail_related", "I do not have that."))
    judge.declined = True

    await runner.rejudge(await _regrade(), on_result=_ignore)

    job_title = next(result for result in graded.results if result.case_id == 1)
    assert job_title.violations == {"declined_answerable": True}


@pytest.mark.usefixtures("judge")
async def test_the_new_run_names_its_judge_and_where_its_answers_came_from(
    graded: Graded,
) -> None:
    await runner.rejudge(await _regrade(), on_result=_ignore)

    [config] = graded.configs
    assert config["judge"] == {"model": "gpt-6.1-sol", "prompt": JUDGE.ref}
    assert config["rejudged_from"] == "baseline"
    # How the answers were written is the source run's record, untouched. That
    # includes the code: `git` is the commit that wrote them, and the commit that
    # graded them again is recorded beside it.
    assert config["chat_model"] == "gpt-5-mini"
    assert config["corpus"] == graded.source_config["corpus"]
    assert config["git"] == {"revision": "old", "dirty": False}
    assert config["rejudged_git"] == {"revision": "abc", "dirty": False}
    # The rubric is named in two places, and both name the new one.
    assert config["prompts"] == ["classifier@2", "rag_agent@1", JUDGE.ref]


@pytest.mark.usefixtures("judge")
async def test_only_grades_the_results_asked_for(graded: Graded) -> None:
    plan = await _regrade(only=frozenset({"hello"}))

    await runner.rejudge(plan, on_result=_ignore)

    assert (plan.subset, plan.graded) == (True, 1)
    assert [result.case_id for result in graded.results] == [2]
    dataset = graded.configs[0]["dataset"]
    assert isinstance(dataset, dict)
    assert (dataset["cases"], dataset["only"]) == (1, ["hello"])


async def test_a_judge_that_fails_on_a_stored_answer_says_why_and_the_rest_go_on(
    graded: Graded, judge: Judge
) -> None:
    judge.error = AssistantError("The assistant is unavailable.")

    record = await runner.rejudge(await _regrade(), on_result=_ignore)

    assert record.status == "complete"
    job_title = next(result for result in graded.results if result.case_id == 1)
    assert job_title.judge_scores is None
    assert job_title.error == "judge: AssistantError: The assistant is unavailable."
    # Nothing of the first judge stays behind as if it had graded this run: not its
    # call, its reasons or what it cost.
    assert [call["step"] for call in job_title.calls] == ["answer"]
    assert (job_title.judge_rationale, job_title.judge_cost) == (None, None)


@pytest.mark.usefixtures("judge")
async def test_an_answer_the_first_judge_failed_on_is_graded_and_the_error_goes(
    graded: Graded,
) -> None:
    """The error recorded beside an answer is the judge's. A new judge that reads the
    answer leaves nothing to report."""
    graded.held[0] = (
        "job-title",
        _held(
            1,
            "mihail_related",
            "He is a Solutions Architect.",
            judge_scores=None,
            judge_rationale=None,
            error="judge: AssistantError: The assistant is unavailable.",
        ),
    )

    await runner.rejudge(await _regrade(), on_result=_ignore)

    job_title = next(result for result in graded.results if result.case_id == 1)
    assert job_title.error is None
    assert job_title.judge_scores is not None


@pytest.mark.parametrize(
    ("change", "arguments", "problem"),
    [
        ({}, {"label": "baseline"}, "already exists"),
        ({}, {"judge_model": "gpt-9"}, "No price for gpt-9"),
        ({}, {"only": frozenset({"nope"})}, "has no result for: nope"),
        ({"status": "failed"}, {}, "is failed, not complete"),
        ({"hash": "another"}, {}, "its cases have changed"),
        ({"fingerprint": "ffffffffffff"}, {}, "knowledge base has changed"),
        ({"passages": "gone"}, {}, "no longer stored under the ids it recorded"),
    ],
    ids=[
        "label-taken",
        "judge-unpriced",
        "unknown-case",
        "source-not-complete",
        "dataset-changed",
        "knowledge-base-changed",
        "knowledge-base-indexed-again",
    ],
)
async def test_a_regrade_that_cannot_be_trusted_is_refused_before_anything_is_spent(
    graded: Graded,
    judge: Judge,
    change: dict[str, str],
    arguments: dict[str, Any],
    problem: str,
) -> None:
    graded.status = change.get("status", graded.status)
    if "hash" in change:
        graded.source_config["dataset"]["content_hash"] = change["hash"]
    if "fingerprint" in change:
        graded.source_config["corpus"]["fingerprint"] = change["fingerprint"]
    if "passages" in change:
        # `ingestion --force`: every passage has a new id, and the fingerprint, which
        # is of what the documents say, is what it was.
        graded.passages.clear()

    with pytest.raises(EvalError, match=problem):
        await _regrade(**arguments)

    assert (graded.configs, judge.asked) == ([], [])


@pytest.mark.usefixtures("graded", "judge")
async def test_a_run_that_does_not_exist_cannot_be_graded_again() -> None:
    with pytest.raises(EvalError, match="no run labelled 'nope'"):
        await runner.prepare_rejudge("nope", label="x", judge_model="gpt-6.1-sol")


@pytest.mark.usefixtures("judge")
async def test_a_passage_that_is_gone_stops_the_regrade_and_marks_it_failed(
    graded: Graded,
) -> None:
    """Every id is looked up before the run is created (the refusal above). This is a
    passage that goes after that, while the answers are being graded: the judge is not
    shown part of what an answer was written from."""
    plan = await _regrade()
    graded.passages.clear()

    with pytest.raises(EvalError, match="no longer in the knowledge base"):
        await runner.rejudge(plan, on_result=_ignore)

    assert graded.finished == [("failed", None)]
