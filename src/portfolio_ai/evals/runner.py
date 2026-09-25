"""Running a dataset against one configuration of the assistant.

Two phases, and the split is the point:

- :func:`prepare` does every check that can refuse a run -- the label is free, the
  cases asked for exist, every document a case expects is actually indexed -- and
  spends nothing. It is all ``--dry-run`` does.
- :func:`execute` stores the dataset, opens the run, answers and grades every case,
  and writes each result as it lands.

**Cases run a few at a time, not one by one and not all at once.** One by one, fifty
answers at twenty seconds each is a quarter of an hour; all at once is fifty
simultaneous calls to OpenAI and fifty searches wanting connections from a pool of
ten. ``asyncio.Semaphore(n)`` is a counter that lets at most ``n`` holders through
``async with`` at once and parks the rest until one leaves.

**The cases run inside an ``asyncio.TaskGroup``**, Python 3.11's structured
concurrency: every task started inside the ``async with`` block has finished when the
block ends, and if one raises, the others are cancelled and the error comes out of
the block. So a case that fails *expectedly* -- OpenAI down for a moment -- is caught
inside the case and recorded, and the run carries on. Anything that would fail every
case the same way -- a rejected API key, a bug -- is allowed out, and ends the run
at once instead of spending on forty-nine more failures.

Nothing here writes to the chat tables: ``agent.respond()`` is the assistant without
memory or storage, which is what lets the evals ask thousands of questions without
leaving a trace where visitors' conversations live.
"""

import asyncio
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- git, with fixed arguments
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from portfolio_ai.assistant import agent
from portfolio_ai.assistant.agent import AssistantConfig, Done, Searched, TurnResult
from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.config import get_settings
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import Corpus, RunRecord, StoredDataset, StoredResult
from portfolio_ai.evals import judge as judging
from portfolio_ai.evals import metrics, report
from portfolio_ai.evals.datasets import Dataset, EvalCase
from portfolio_ai.exceptions import ConfigError, EvalError, PortfolioAIError
from portfolio_ai.llm import pricing

# Needed only by annotations on local variables, which Python never evaluates, so
# they are imported for mypy alone. TYPE_CHECKING is False when the program runs
# and True while a type checker reads the file.
if TYPE_CHECKING:
    from decimal import Decimal

    from portfolio_ai.db.documents import RetrievedChunk

log = structlog.get_logger(__name__)

# Enough to finish a fifty-case run in a few minutes, few enough to stay inside the
# connection pool and OpenAI's rate limits with room to spare.
DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class Plan:
    """Everything decided before the first question is asked."""

    dataset: Dataset
    cases: tuple[EvalCase, ...]
    label: str
    config: AssistantConfig
    # None when the run was asked for without a judge (--no-judge).
    judge_model: str | None
    concurrency: int
    corpus: Corpus
    # What the database already holds under the dataset's name.
    stored: StoredDataset | None

    @property
    def subset(self) -> bool:
        return len(self.cases) < len(self.dataset.cases)

    @property
    def dataset_state(self) -> str:
        """The dataset's standing, in words, for the plan the dry run prints."""
        if self.stored is None:
            return "new: stored when the run starts, and frozen once it completes"
        if self.stored.content_hash != self.dataset.content_hash:
            return "changed: its stored cases will be replaced, and frozen once this completes"
        if self.stored.complete_runs:
            return f"unchanged, frozen by {self.stored.complete_runs} complete run(s)"
        return "unchanged, and frozen once this run completes"

    def record(self) -> dict[str, object]:
        """The run's configuration, as stored in ``eval_runs.config``.

        Everything that could change a score is here, so two runs months apart can
        be told apart by what differed: models, efforts, retrieval, every prompt
        version, the judge, the knowledge base's fingerprint, and the code itself.
        """
        return {
            **self.config.as_record(),
            "embedding_model": get_settings().embedding_model,
            "judge": {"model": self.judge_model, "prompt": JUDGE.ref} if self.judge_model else None,
            "corpus": self.corpus.as_record(),
            "dataset": {
                "name": self.dataset.name,
                "content_hash": self.dataset.content_hash,
                "cases": len(self.cases),
                "only": [case.key for case in self.cases] if self.subset else None,
            },
            "concurrency": self.concurrency,
            "git": git_revision(),
        }


@dataclass(frozen=True)
class Progress:
    """One case finished; what the command line prints as it happens."""

    done: int
    total: int
    key: str
    result: StoredResult


def git_revision() -> dict[str, object] | None:
    """The commit the code under test came from, and whether it had changes on top.

    ``subprocess.run`` starts a program and waits for it. ``capture_output`` keeps its
    output instead of printing it, ``text`` decodes it to ``str``, and ``check`` turns
    a non-zero exit into an exception. ``shutil.which`` resolves the program's full
    path first. On Linux and macOS that also means a ``git`` planted in the current
    directory is not the one run. Windows searches the current directory first unless
    the ``NoDefaultCurrentDirectoryInExePath`` environment variable is set, and it is not
    set by default. None when git is missing or this is not a checkout.
    """
    git = shutil.which("git")
    if git is None:
        return None

    def output(*arguments: str) -> str:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- fixed arguments
            [git, *arguments], capture_output=True, text=True, check=True, timeout=10
        ).stdout

    try:
        head = output("rev-parse", "--short=12", "HEAD").strip()
        status = output("status", "--porcelain")
    except (OSError, subprocess.SubprocessError):
        return None
    return {"revision": head, "dirty": bool(status.strip())}


async def prepare(  # ruff: ignore[too-many-arguments] -- keyword-only; see responses.parse
    dataset: Dataset,
    *,
    label: str,
    config: AssistantConfig,
    judge_model: str | None,
    concurrency: int = DEFAULT_CONCURRENCY,
    only: frozenset[str] = frozenset(),
) -> Plan:
    """Every check that can refuse a run, before anything is spent or stored."""
    # A misspelt model fails on every call it makes, and OpenAI's "no such model" is an
    # ordinary per-case failure, so the run would carry on: fifty-four answers paid for
    # and none graded, the label used up, and the dataset frozen.
    models = [config.chat_model, config.classifier_model, *([judge_model] if judge_model else [])]
    unpriced = sorted({model for model in models if not pricing.is_priced(model)})
    if unpriced:
        raise EvalError(
            f"No price for {', '.join(unpriced)} in llm/pricing.py: a typo, or a model to add "
            "there first. Its cost would otherwise read as $0."
        )

    if await evals_db.label_taken(label):
        raise EvalError(f"A run labelled {label!r} already exists. Pick another label.")

    # The same check sync_dataset makes when the run starts, read-only here so a dry run
    # says whether this dataset can run at all.
    stored = await evals_db.stored_dataset(dataset.name)
    if stored and stored.complete_runs and stored.content_hash != dataset.content_hash:
        raise evals_db.frozen_error(dataset.name)

    unknown = sorted(only - {case.key for case in dataset.cases})
    if unknown:
        raise EvalError(f"{dataset.name} has no case called: {', '.join(unknown)}")
    cases = tuple(case for case in dataset.cases if not only or case.key in only)

    corpus = await evals_db.corpus_state()
    missing = sorted({doc for case in cases for doc in case.expected_doc_ids} - corpus.doc_ids)
    if missing:
        # Most likely the local knowledge base is behind GitHub, or a doc_id in the
        # dataset is misspelt. Either way every case expecting it would score as a
        # retrieval miss that is nothing to do with retrieval.
        raise EvalError(
            f"{dataset.name} expects documents that are not indexed: {', '.join(missing)}. "
            "Run the ingestion (python -m portfolio_ai.ingestion), or fix the doc_id."
        )

    return Plan(
        dataset=dataset,
        cases=cases,
        label=label,
        config=config,
        judge_model=judge_model,
        concurrency=concurrency,
        corpus=corpus,
        stored=stored,
    )


async def execute(plan: Plan, *, on_result: Callable[[Progress], None]) -> RunRecord:
    """Run every case in the plan, store each result, and total the run."""
    synced = await evals_db.sync_dataset(plan.dataset)
    run_id = await evals_db.create_run(dataset_id=synced.id, label=plan.label, config=plan.record())
    log.info("eval_run_started", label=plan.label, cases=len(plan.cases))

    gate = asyncio.Semaphore(plan.concurrency)
    finished = 0

    async def one(case: EvalCase) -> None:
        # `nonlocal` lets this nested function assign to `finished` in the enclosing
        # one; without it, `finished += 1` would create a new local variable here.
        nonlocal finished
        async with gate:
            result = await evaluate(
                case,
                case_id=synced.case_ids[case.key],
                config=plan.config,
                judge_model=plan.judge_model,
            )
        await evals_db.insert_result(run_id, result)
        finished += 1
        on_result(Progress(done=finished, total=len(plan.cases), key=case.key, result=result))

    try:
        async with asyncio.TaskGroup() as group:
            for case in plan.cases:
                group.create_task(one(case))
    except BaseException as exc:
        # Ctrl-C arrives here as a cancellation; a rejected key as the ConfigError
        # that stopped the group. Either way the run is marked failed rather than
        # left "running" for ever.
        await _mark_failed(run_id)
        # A TaskGroup reports what its tasks raised as an ExceptionGroup, which can
        # hold several. When it is one of this project's errors, raise that one
        # itself: its message was written for a person, and the group's is not.
        if isinstance(exc, BaseExceptionGroup):
            ours = [error for error in exc.exceptions if isinstance(error, PortfolioAIError)]
            if ours:
                raise ours[0] from exc
        raise

    rows = await evals_db.results(run_id)
    await evals_db.finish_run(run_id, status="complete", totals=report.totals(rows))
    log.info("eval_run_finished", label=plan.label, cases=len(rows))

    record = await evals_db.find_run(plan.label)
    if record is None:  # pragma: no cover - it was created above
        raise RuntimeError(f"the run {plan.label!r} was stored but cannot be read back")
    return record


async def _mark_failed(run_id: int) -> None:
    """Record the failure, without letting a second failure hide the first."""
    try:
        await evals_db.finish_run(run_id, status="failed", totals=None)
    except Exception:  # the database may be the reason the run failed
        log.exception("eval_run_not_marked_failed", run_id=run_id)


async def evaluate(
    case: EvalCase, *, case_id: int, config: AssistantConfig, judge_model: str | None
) -> StoredResult:
    """Answer one case, measure the answer, and have it graded."""
    chunks: list[RetrievedChunk] = []
    done: TurnResult | None = None

    try:
        async for event in agent.respond(case.question, case.history_messages(), config=config):
            if isinstance(event, Searched):
                chunks.extend(event.chunks)
            elif isinstance(event, Done):
                done = event.result
    except ConfigError:
        raise  # a rejected key fails every case the same way: stop the run
    except PortfolioAIError as exc:
        return _unanswered(case_id, f"answer: {type(exc).__name__}: {exc}")

    if done is None:  # pragma: no cover - respond() always ends with Done
        raise AssertionError("the assistant finished without an answer")

    shown = metrics.ranked_documents(chunks)
    scores = metrics.retrieval_scores(case.expected_doc_ids, shown)
    violations = metrics.rule_violations(
        case,
        route=done.classification,
        answer=done.reply,
        links_removed=done.links_removed,
        chunks=chunks,
    )

    verdict: judging.Verdict | None = None
    judge_cost: Decimal | None = None
    problem: str | None = None
    calls = [call.as_record() for call in done.calls]

    # The out-of-scope reply is fixed text; there is nothing in it to grade.
    if judge_model and done.classification != "out_of_scope":
        try:
            judgement = await judging.judge(
                case, route=done.classification, chunks=chunks, answer=done.reply, model=judge_model
            )
        except ConfigError:
            raise
        except PortfolioAIError as exc:
            problem = f"judge: {type(exc).__name__}: {exc}"
        else:
            verdict, judge_cost = judgement.verdict, judgement.usage.cost
            calls.append(judgement.usage.as_record())
            if judgement.problem:
                problem = f"judge: {judgement.problem}"

    violations |= metrics.fallback_violations(
        case,
        route=done.classification,
        fallback_used=done.fallback_used,
        declined=verdict.declined if verdict else None,
    )

    return StoredResult(
        case_id=case_id,
        answer=done.reply,
        classification=done.classification,
        retrieved_chunk_ids=done.retrieved_chunk_ids,
        retrieved_doc_ids=shown,
        recall=scores.recall if scores else None,
        precision=scores.precision if scores else None,
        mrr=scores.mrr if scores else None,
        judge_scores=verdict.as_record() if verdict else None,
        judge_rationale=verdict.rationale if verdict else None,
        violations=violations,
        prompt_tokens=done.prompt_tokens,
        completion_tokens=done.completion_tokens,
        cost=done.cost,
        judge_cost=judge_cost,
        latency_ms=done.latency_ms,
        first_token_ms=done.first_token_ms,
        top_score=done.top_score,
        fallback_used=done.fallback_used,
        links_removed=done.links_removed,
        searches=[search.as_record() for search in done.searches],
        calls=calls,
        error=problem,
    )


def _unanswered(case_id: int, error: str) -> StoredResult:
    """A case the assistant could not answer: recorded, so the run can go on."""
    return StoredResult(
        case_id=case_id,
        answer=None,
        classification=None,
        retrieved_chunk_ids=[],
        retrieved_doc_ids=[],
        recall=None,
        precision=None,
        mrr=None,
        judge_scores=None,
        judge_rationale=None,
        violations={},
        prompt_tokens=None,
        completion_tokens=None,
        cost=None,
        judge_cost=None,
        latency_ms=None,
        first_token_ms=None,
        top_score=None,
        fallback_used=None,
        links_removed=None,
        searches=[],
        calls=[],
        error=error,
    )
