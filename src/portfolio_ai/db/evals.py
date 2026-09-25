"""Every statement that touches the eval tables, plus the corpus fingerprint a run records.

Four tables, in the shape migrations 0003 and 0005 give them: a dataset holds cases,
a run applies one configuration to one dataset, and a result is one case under one
run. Everything an eval command reads or writes goes through here.

The one rule this module enforces rather than stores: **a dataset with a complete
run is frozen.** Scores are only comparable across runs of identical cases, so
:func:`sync_dataset` refuses a changed file once the dataset it names has been
scored, instead of quietly re-seeding it under the same name. A run that failed
part-way does not count: there are no scores of it worth protecting.
"""

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from psycopg import errors
from psycopg.types.json import Jsonb

from portfolio_ai.db.documents import load_index
from portfolio_ai.db.pool import get_pool
from portfolio_ai.evals.datasets import Dataset
from portfolio_ai.exceptions import EvalError


@dataclass(frozen=True)
class Corpus:
    """The knowledge base a run was scored against."""

    documents: int
    chunks: int
    # Twelve hex characters of a hash over every document's id and content hash:
    # the same knowledge base always gives the same fingerprint, and a changed
    # article gives a different one. Two runs with different fingerprints differ in
    # what they could retrieve, not only in how.
    fingerprint: str
    doc_ids: frozenset[str]

    def as_record(self) -> dict[str, object]:
        return {"documents": self.documents, "chunks": self.chunks, "fingerprint": self.fingerprint}


@dataclass(frozen=True)
class SyncedDataset:
    id: int
    # Each case's database id, by key.
    case_ids: dict[str, int]
    # "created", "unchanged" or "replaced".
    state: str


@dataclass(frozen=True)
class StoredDataset:
    """What the database holds for a dataset's name, read without changing anything."""

    content_hash: str | None
    # Complete runs only: they are what freeze it.
    complete_runs: int


@dataclass(frozen=True)
class StoredResult:
    """One case's result, in the shape it is written."""

    case_id: int
    answer: str | None
    classification: str | None
    retrieved_chunk_ids: list[int]
    retrieved_doc_ids: list[str]
    recall: float | None
    precision: float | None
    mrr: float | None
    judge_scores: dict[str, object] | None
    judge_rationale: str | None
    violations: dict[str, object]
    prompt_tokens: int | None
    completion_tokens: int | None
    cost: Decimal | None
    judge_cost: Decimal | None
    latency_ms: int | None
    first_token_ms: int | None
    top_score: float | None
    fallback_used: bool | None
    links_removed: int | None
    searches: list[dict[str, object]]
    # One record per OpenAI call behind the result, the judge's included: step,
    # model, tokens (reasoning separately), latency and cost.
    calls: list[dict[str, object]]
    error: str | None


@dataclass(frozen=True)
class ResultRow:
    """One case's result as read back, with the case it belongs to."""

    case_id: int
    key: str
    question: str
    category: str
    also_accept: list[str]
    expected_doc_ids: list[str]
    expect_fallback: bool
    answer: str | None
    route: str | None
    retrieved_doc_ids: list[str]
    recall: float | None
    precision: float | None
    mrr: float | None
    judge_scores: dict[str, Any] | None
    judge_rationale: str | None
    violations: dict[str, Any]
    prompt_tokens: int | None
    completion_tokens: int | None
    cost: Decimal | None
    judge_cost: Decimal | None
    latency_ms: int | None
    first_token_ms: int | None
    top_score: float | None
    fallback_used: bool | None
    calls: list[dict[str, Any]]
    error: str | None


@dataclass(frozen=True)
class RunRecord:
    id: int
    label: str
    dataset_id: int
    dataset: str
    status: str
    config: dict[str, Any]
    totals: dict[str, Any] | None
    started_at: dt.datetime
    finished_at: dt.datetime | None


async def corpus_state() -> Corpus:
    """What is indexed right now: counts, a fingerprint, and every document id."""
    index = await load_index()
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select count(*) as chunks from chunks")
        row = await cur.fetchone()

    lines = sorted(f"{doc.doc_id}:{doc.content_hash}" for doc in index.by_doc_id.values())
    fingerprint = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]
    return Corpus(
        documents=len(index),
        chunks=int(row["chunks"]) if row else 0,
        fingerprint=fingerprint,
        doc_ids=frozenset(index.by_doc_id),
    )


def frozen_error(name: str) -> EvalError:
    """The refusal for a changed dataset that already has a complete run."""
    return EvalError(
        f"{name} has changed since it was run, and a dataset with a complete run is frozen: "
        "scores are only comparable on identical cases. Copy the file to a new name "
        "(golden_v2, say) for the change."
    )


async def stored_dataset(name: str) -> StoredDataset | None:
    """What is stored under ``name``, if anything. Reads only: the dry run uses it."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select d.content_hash,
                   count(r.id) filter (where r.status = 'complete') as complete_runs
            from eval_datasets d
            left join eval_runs r on r.dataset_id = d.id
            where d.name = %s
            group by d.id
            """,
            (name,),
        )
        row = await cur.fetchone()

    if row is None:
        return None
    return StoredDataset(content_hash=row["content_hash"], complete_runs=int(row["complete_runs"]))


async def sync_dataset(dataset: Dataset) -> SyncedDataset:
    """Make the stored copy of ``dataset`` match the file, or refuse.

    - Not stored yet: stored.
    - Stored with the same content hash: nothing to do.
    - Stored, changed, and never completely run: its cases are replaced. This is the
      authoring phase, when edits should be free. The results of any run that failed
      part-way go with the old cases (``on delete cascade``); the run itself stays,
      empty, and keeps its label.
    - Stored, changed, and completely run at least once: :class:`EvalError`.

    One transaction, and ``for update`` on the dataset's row, so two runs starting at
    once cannot both decide to replace the cases.
    """
    content_hash = dataset.content_hash
    pool = await get_pool()

    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(
            """
            select id, content_hash,
                   exists(
                       select 1 from eval_runs r
                       where r.dataset_id = d.id and r.status = 'complete'
                   ) as has_runs
            from eval_datasets d
            where name = %s
            for update
            """,
            (dataset.name,),
        )
        row = await cur.fetchone()

        if row is None:
            await cur.execute(
                """
                insert into eval_datasets (name, description, content_hash)
                values (%s, %s, %s)
                returning id
                """,
                (dataset.name, dataset.description, content_hash),
            )
            created = await cur.fetchone()
            if created is None:  # pragma: no cover - insert ... returning always returns
                raise RuntimeError("the dataset was inserted but its id did not come back")
            dataset_id, state = int(created["id"]), "created"
        elif row["content_hash"] == content_hash:
            dataset_id, state = int(row["id"]), "unchanged"
        elif row["has_runs"]:
            raise frozen_error(dataset.name)
        else:
            dataset_id, state = int(row["id"]), "replaced"
            await cur.execute("delete from eval_cases where dataset_id = %s", (dataset_id,))
            await cur.execute(
                "update eval_datasets set description = %s, content_hash = %s where id = %s",
                (dataset.description, content_hash, dataset_id),
            )

        if state != "unchanged":
            # executemany sends every row in one pipeline rather than one round trip
            # per case -- the database is across the network (see CLAUDE.md).
            await cur.executemany(
                """
                insert into eval_cases (
                    dataset_id, key, question, category, also_accept, history,
                    expected_doc_ids, expect_fallback, reference_answer,
                    must_include, must_not_include, notes
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        dataset_id,
                        case.key,
                        case.question,
                        case.category,
                        list(case.also_accept),
                        Jsonb([turn.model_dump() for turn in case.history]),
                        list(case.expected_doc_ids),
                        case.expect_fallback,
                        case.reference_answer,
                        list(case.must_include),
                        list(case.must_not_include),
                        case.notes,
                    )
                    for case in dataset.cases
                ],
            )

        await cur.execute("select key, id from eval_cases where dataset_id = %s", (dataset_id,))
        case_ids = {str(case["key"]): int(case["id"]) for case in await cur.fetchall()}

    return SyncedDataset(id=dataset_id, case_ids=case_ids, state=state)


async def label_taken(label: str) -> bool:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("select 1 as taken from eval_runs where label = %s", (label,))
        return await cur.fetchone() is not None


async def create_run(*, dataset_id: int, label: str, config: dict[str, object]) -> int:
    """Start a run. Its status stays ``running`` until :func:`finish_run`."""
    pool = await get_pool()
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                insert into eval_runs (dataset_id, label, config)
                values (%s, %s, %s)
                returning id
                """,
                (dataset_id, label, Jsonb(config)),
            )
            row = await cur.fetchone()
    except errors.UniqueViolation as exc:
        # Checked before anything is spent (label_taken), so this is the race: two
        # runs started with one label at once.
        raise EvalError(f"A run labelled {label!r} already exists. Pick another label.") from exc

    if row is None:  # pragma: no cover - insert ... returning always returns
        raise RuntimeError("the run was inserted but its id did not come back")
    return int(row["id"])


async def insert_result(run_id: int, result: StoredResult) -> None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            insert into eval_results (
                run_id, case_id, answer, classification,
                retrieved_chunk_ids, retrieved_doc_ids, recall_at_k, precision_at_k, mrr,
                judge_scores, judge_rationale, rule_violations,
                prompt_tokens, completion_tokens, cost_usd, judge_cost_usd,
                latency_ms, first_token_ms, top_score, fallback_used, links_removed,
                searches, calls, error
            )
            values (
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            """,
            (
                run_id,
                result.case_id,
                result.answer,
                result.classification,
                result.retrieved_chunk_ids,
                result.retrieved_doc_ids,
                result.recall,
                result.precision,
                result.mrr,
                Jsonb(result.judge_scores) if result.judge_scores is not None else None,
                result.judge_rationale,
                Jsonb(result.violations),
                result.prompt_tokens,
                result.completion_tokens,
                result.cost,
                result.judge_cost,
                result.latency_ms,
                result.first_token_ms,
                result.top_score,
                result.fallback_used,
                result.links_removed,
                Jsonb(result.searches),
                Jsonb(result.calls),
                result.error,
            ),
        )


async def finish_run(run_id: int, *, status: str, totals: dict[str, object] | None) -> None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            update eval_runs
            set status = %s, totals = %s, finished_at = now()
            where id = %s
            """,
            (status, Jsonb(totals) if totals is not None else None, run_id),
        )


async def results(run_id: int) -> list[ResultRow]:
    """A run's results with their cases, in the dataset's order."""
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select c.id as case_id, c.key, c.question, c.category, c.also_accept,
                   c.expected_doc_ids, c.expect_fallback,
                   r.answer, r.classification, r.retrieved_doc_ids,
                   r.recall_at_k, r.precision_at_k, r.mrr,
                   r.judge_scores, r.judge_rationale, r.rule_violations,
                   r.prompt_tokens, r.completion_tokens, r.cost_usd, r.judge_cost_usd,
                   r.latency_ms, r.first_token_ms, r.top_score, r.fallback_used, r.calls,
                   r.error
            from eval_results r
            join eval_cases c on c.id = r.case_id
            where r.run_id = %s
            order by c.id
            """,
            (run_id,),
        )
        rows = await cur.fetchall()

    return [
        ResultRow(
            case_id=row["case_id"],
            key=row["key"],
            question=row["question"],
            category=row["category"],
            also_accept=list(row["also_accept"]),
            expected_doc_ids=list(row["expected_doc_ids"]),
            expect_fallback=row["expect_fallback"],
            answer=row["answer"],
            route=row["classification"],
            retrieved_doc_ids=list(row["retrieved_doc_ids"] or []),
            recall=row["recall_at_k"],
            precision=row["precision_at_k"],
            mrr=row["mrr"],
            judge_scores=row["judge_scores"],
            judge_rationale=row["judge_rationale"],
            violations=row["rule_violations"] or {},
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cost=row["cost_usd"],
            judge_cost=row["judge_cost_usd"],
            latency_ms=row["latency_ms"],
            first_token_ms=row["first_token_ms"],
            top_score=row["top_score"],
            fallback_used=row["fallback_used"],
            calls=list(row["calls"] or []),
            error=row["error"],
        )
        for row in rows
    ]


_RUN_COLUMNS = """
    select r.id, r.label, r.dataset_id, d.name as dataset, r.status, r.config, r.totals,
           r.started_at, r.finished_at
    from eval_runs r
    join eval_datasets d on d.id = r.dataset_id
"""


def _run(row: dict[str, Any]) -> RunRecord:
    return RunRecord(
        id=row["id"],
        label=row["label"],
        dataset_id=row["dataset_id"],
        dataset=row["dataset"],
        status=row["status"],
        config=row["config"],
        totals=row["totals"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


async def find_run(label: str) -> RunRecord | None:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_RUN_COLUMNS + "where r.label = %s", (label,))
        row = await cur.fetchone()
    return _run(row) if row else None


async def list_runs() -> list[RunRecord]:
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_RUN_COLUMNS + "order by r.started_at")
        return [_run(row) for row in await cur.fetchall()]
