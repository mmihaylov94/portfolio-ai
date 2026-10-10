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

**A second pair does the same for the judge alone.** :func:`prepare_rejudge` and
:func:`rejudge` take a run that is already stored and have the judge read its
answers again, as a new run. The answers are not asked again: the ruler changes and
what it measures does not. That is what makes a new judge or a new rubric usable,
because a run graded by one judge cannot be compared with a run graded by another.
"""

import asyncio
import dataclasses
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- git, with fixed arguments
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import structlog

from portfolio_ai.assistant import agent
from portfolio_ai.assistant.agent import AssistantConfig, Done, Searched, TurnResult
from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.config import effort_problem, get_settings
from portfolio_ai.db import documents as documents_db
from portfolio_ai.db import evals as evals_db
from portfolio_ai.db.evals import Corpus, RunRecord, StoredDataset, StoredResult
from portfolio_ai.evals import datasets, metrics, report
from portfolio_ai.evals import judge as judging
from portfolio_ai.evals.datasets import Dataset, EvalCase
from portfolio_ai.exceptions import ConfigError, EvalError, PortfolioAIError
from portfolio_ai.llm import pricing

# Needed only by annotations on local variables, which Python never evaluates, so
# it is imported for mypy alone. TYPE_CHECKING is False when the program runs and
# True while a type checker reads the file.
if TYPE_CHECKING:
    from decimal import Decimal

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
    # ordinary per-case failure, so the run would carry on: every answer paid for and
    # none graded, the label used up, and the dataset frozen.
    models = [config.chat_model, config.classifier_model, *([judge_model] if judge_model else [])]
    unpriced = sorted({model for model in models if not pricing.is_priced(model)})
    if unpriced:
        raise EvalError(
            f"No price for {', '.join(unpriced)} in llm/pricing.py: a typo, or a model to add "
            "there first. Its cost would otherwise read as $0."
        )

    # --chat-model and --chat-effort can each be given without the other, and a pair
    # that does not go together is refused by OpenAI on every call: sixty-two failed
    # cases and a label used up. The settings make the same check for .env.
    for model, effort in (
        (config.chat_model, config.chat_effort),
        (config.classifier_model, config.classifier_effort),
    ):
        problem = effort_problem(model, effort)
        if problem:
            raise EvalError(f"{problem} Give the model and its effort together.")

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

    # partial(one, case) is `one` with its argument already filled in: something that
    # makes the coroutine when called, rather than a coroutine made now and perhaps
    # never run.
    await _run_all(run_id, [partial(one, case) for case in plan.cases])
    return await _complete(run_id, plan.label)


async def _run_all(run_id: int, jobs: Sequence[Callable[[], Coroutine[Any, Any, None]]]) -> None:
    """Run every job, a failure in one ending them all and marking the run failed."""
    try:
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(job())
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


async def _complete(run_id: int, label: str) -> RunRecord:
    """Total the stored results, mark the run complete, and read it back."""
    rows = await evals_db.results(run_id)
    await evals_db.finish_run(run_id, status="complete", totals=report.totals(rows))
    log.info("eval_run_finished", label=label, cases=len(rows))

    record = await evals_db.find_run(label)
    if record is None:  # pragma: no cover - it was created above
        raise RuntimeError(f"the run {label!r} was stored but cannot be read back")
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
    chunks: list[documents_db.RetrievedChunk] = []
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
        judgement, problem = await _grade(
            case, route=done.classification, chunks=chunks, answer=done.reply, model=judge_model
        )
        if judgement is not None:
            verdict, judge_cost = judgement.verdict, judgement.usage.cost
            calls.append(judgement.usage.as_record())

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


async def _grade(
    case: EvalCase,
    *,
    route: str,
    chunks: Sequence[documents_db.RetrievedChunk],
    answer: str,
    model: str,
) -> tuple[judging.Judgement | None, str | None]:
    """Have the judge read one answer. Returns its judgement, and what went wrong if anything.

    A judge that fails is a problem recorded beside the answer, and the run goes on. A
    rejected key is not: it fails every call the same way, so it is let out to stop the
    run, here as everywhere.
    """
    try:
        judgement = await judging.judge(
            case, route=route, chunks=chunks, answer=answer, model=model
        )
    except ConfigError:
        raise
    except PortfolioAIError as exc:
        return None, f"judge: {type(exc).__name__}: {exc}"
    return judgement, f"judge: {judgement.problem}" if judgement.problem else None


# --- grading a stored run again -------------------------------------------------


def _gradable(result: StoredResult) -> bool:
    """Whether the judge reads this result: it has an answer, and the answer is not
    the fixed out-of-scope reply, which has nothing in it to grade."""
    return result.answer is not None and result.classification not in {None, "out_of_scope"}


@dataclass(frozen=True)
class Regrade:
    """Everything decided before a stored run's answers are graded again."""

    source: RunRecord
    label: str
    judge_model: str
    concurrency: int
    # Each case with the result the source run stored for it, in the dataset's order.
    items: tuple[tuple[EvalCase, StoredResult], ...]
    # True when only some of the source run's results were asked for (--only).
    subset: bool

    @property
    def graded(self) -> int:
        """How many answers the judge will read. The rest are copied as they are."""
        return sum(_gradable(result) for _, result in self.items)

    def record(self) -> dict[str, object]:
        """The new run's configuration: the source run's, with the judge replaced.

        Everything about how the answers were written stays as the source recorded
        it, because they are the same answers. That includes ``git``: it is the code
        that wrote them. The code that graded them again goes in ``rejudged_git``.
        ``rejudged_from`` is how a reader tells this run's answers were not asked again.
        """
        dataset = dict(self.source.config.get("dataset") or {})
        if self.subset:
            dataset |= {"cases": len(self.items), "only": [case.key for case, _ in self.items]}
        # The source's list of prompt versions names the rubric it was graded with.
        # Left as it was, the new run would say judge@2 in one place and judge@1 in another.
        prompts = [
            JUDGE.ref if str(ref).startswith(f"{JUDGE.name}@") else ref
            for ref in self.source.config.get("prompts") or []
        ]
        return {
            **self.source.config,
            "prompts": prompts,
            "judge": {"model": self.judge_model, "prompt": JUDGE.ref},
            "dataset": dataset,
            "concurrency": self.concurrency,
            "rejudged_from": self.source.label,
            "rejudged_git": git_revision(),
        }


async def prepare_rejudge(
    source_label: str,
    *,
    label: str,
    judge_model: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    only: frozenset[str] = frozenset(),
) -> Regrade:
    """Every check that can refuse a re-grade, before anything is spent or stored."""
    source = await evals_db.find_run(source_label)
    if source is None:
        raise EvalError(f"There is no run labelled {source_label!r}. `list` shows the runs.")
    if source.status != "complete":
        raise EvalError(
            f"{source_label} is {source.status}, not complete: it has no finished set of "
            "answers to grade again."
        )

    if not pricing.is_priced(judge_model):
        raise EvalError(
            f"No price for {judge_model} in llm/pricing.py: a typo, or a model to add there "
            "first. Its cost would otherwise read as $0."
        )
    if await evals_db.label_taken(label):
        raise EvalError(f"A run labelled {label!r} already exists. Pick another label.")

    # The judge is shown each case's reference answer and earlier conversation, which
    # live in the dataset file. It has to be the file the source run was asked from.
    dataset = datasets.load(datasets.path_for(source.dataset))
    recorded = (source.config.get("dataset") or {}).get("content_hash")
    if recorded != dataset.content_hash:
        raise EvalError(
            f"{datasets.path_for(source.dataset)} is not the file {source_label} ran: its "
            "cases have changed since. The answers cannot be graded against other cases."
        )

    # The judge is also shown the passages each answer was shown, and a stored result
    # holds only their ids. Two checks, because two things can come between an id and
    # its text. This one is the text: a document that has changed since.
    corpus = await evals_db.corpus_state()
    then = (source.config.get("corpus") or {}).get("fingerprint")
    if corpus.fingerprint != then:
        raise EvalError(
            f"The knowledge base has changed since {source_label} ran (fingerprint {then} "
            f"then, {corpus.fingerprint} now), so the passages its answers were shown can "
            "no longer be read back. Run the dataset again instead."
        )

    stored = await evals_db.stored_results(source.id)
    unknown = sorted(only - {key for key, _ in stored})
    if unknown:
        raise EvalError(f"{source_label} has no result for: {', '.join(unknown)}")

    cases = {case.key: case for case in dataset.cases}
    items = tuple((cases[key], result) for key, result in stored if not only or key in only)

    # And this one is the ids. The fingerprint is of what the documents say, and an id
    # is not part of that: `ingestion --force` indexes unchanged documents again, which
    # gives every chunk a new id and leaves the fingerprint as it was. So the ids are
    # looked up here, in one query, while there is still no run to mark as failed.
    wanted = {
        chunk_id
        for _, result in items
        if _gradable(result)
        for chunk_id in result.retrieved_chunk_ids
    }
    held = {chunk.chunk_id for chunk in await documents_db.chunks_by_id(sorted(wanted))}
    if wanted - held:
        raise EvalError(
            f"{len(wanted - held)} of the {len(wanted)} passages {source_label}'s answers were "
            "shown are no longer stored under the ids it recorded. The knowledge base has "
            "been indexed again since, which gives a passage a new id even when its text is "
            "the same. Run the dataset again instead."
        )

    return Regrade(
        source=source,
        label=label,
        judge_model=judge_model,
        concurrency=concurrency,
        items=items,
        subset=len(items) < len(stored),
    )


async def rejudge(plan: Regrade, *, on_result: Callable[[Progress], None]) -> RunRecord:
    """Grade the stored answers again, and store them as a new run of the same dataset."""
    run_id = await evals_db.create_run(
        dataset_id=plan.source.dataset_id, label=plan.label, config=plan.record()
    )
    log.info(
        "eval_rejudge_started", label=plan.label, source=plan.source.label, answers=plan.graded
    )

    gate = asyncio.Semaphore(plan.concurrency)
    finished = 0

    async def one(case: EvalCase, stored: StoredResult) -> None:
        nonlocal finished
        async with gate:
            result = await regrade(case, stored, judge_model=plan.judge_model)
        await evals_db.insert_result(run_id, result)
        finished += 1
        on_result(Progress(done=finished, total=len(plan.items), key=case.key, result=result))

    await _run_all(run_id, [partial(one, case, stored) for case, stored in plan.items])
    return await _complete(run_id, plan.label)


async def regrade(case: EvalCase, stored: StoredResult, *, judge_model: str) -> StoredResult:
    """One stored result with the judge's part replaced, and everything else kept.

    ``dataclasses.replace`` builds a copy of a frozen dataclass with the named fields
    changed. The answer, the route, what was retrieved, the rule checks, the timings
    and what the answer cost are the source run's, because it is the same answer.
    """
    answer, route = stored.answer, stored.classification
    if answer is None or route is None or route == "out_of_scope":
        return stored

    chunks = await documents_db.chunks_by_id(stored.retrieved_chunk_ids)
    # prepare_rejudge has already looked every id up. This is for a passage that went
    # in between: an ingestion that ran while the answers were being graded.
    if len(chunks) != len(stored.retrieved_chunk_ids):
        gone = len(stored.retrieved_chunk_ids) - len(chunks)
        raise EvalError(
            f"{case.key}: {gone} of the {len(stored.retrieved_chunk_ids)} passages this answer "
            "was shown are no longer in the knowledge base, so the judge cannot be shown "
            "what the answer was written from."
        )

    judgement, problem = await _grade(
        case, route=route, chunks=chunks, answer=answer, model=judge_model
    )
    verdict = judgement.verdict if judgement else None

    # Whether an answer declined is the judge's reading, so the two findings that rest
    # on it are worked out again. Every other finding is about the answer's text.
    violations = {
        rule: detail
        for rule, detail in stored.violations.items()
        if rule not in metrics.FALLBACK_RULES
    }
    violations |= metrics.fallback_violations(
        case,
        route=route,
        fallback_used=bool(stored.fallback_used),
        declined=verdict.declined if verdict else None,
    )

    calls = [call for call in stored.calls if call.get("step") != "judge"]
    if judgement is not None:
        calls.append(judgement.usage.as_record())

    return dataclasses.replace(
        stored,
        judge_scores=verdict.as_record() if verdict else None,
        judge_rationale=verdict.rationale if verdict else None,
        judge_cost=judgement.usage.cost if judgement else None,
        violations=violations,
        calls=calls,
        error=problem,
    )
