"""Turning a run's results into numbers, and numbers into something worth reading.

:func:`totals` is the one place a run's results are summarised. It works on rows as
read back from the database, so the totals stored with a run and anything computed
later from its rows can never disagree.

The rest formats. Plain text built with f-string widths rather than a table library:
the output is a few dozen lines, and a format spec such as ``{value:>8.3f}`` -- right
aligned, eight wide, three decimals -- is all the layout it needs.

**Read comparisons case by case.** Models do not answer the same way twice, and on
fifty cases one answer changing moves an average by two points. So :func:`compare`
lists the cases that changed as well as the averages. An average that moved while
no case changed much is noise; three cases whose faithfulness fell from 5 to 2 is a
finding.
"""

import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from portfolio_ai.db.evals import ResultRow, RunRecord

type Totals = dict[str, Any]

JUDGED = ("faithfulness", "completeness", "style")
# A judge score has to move this far for a case to be listed as changed. One point
# either way is within what the same judge gives the same answer on another day.
SCORE_CHANGE = 2
# At or below this, a judge score is listed among a case's problems.
LOW_SCORE = 2
# Two runs side by side get a third column: the change from the first to the second.
PAIR = 2
# How much of an answer `show --failures` prints before cutting it short.
ANSWER_PREVIEW = 300


def _mean(values: Sequence[float]) -> float | None:
    return round(statistics.fmean(values), 3) if values else None


def _share(hits: int, of: int) -> float | None:
    return round(hits / of, 3) if of else None


def _percentiles(values: Sequence[int]) -> dict[str, int | None]:
    """The median and the 95th percentile.

    ``statistics.quantiles`` needs at least two values; with ``n=20`` it returns the
    19 cut points between twenty equal groups, so index 18 is the 95th percentile.
    ``method="inclusive"`` treats the data as the whole population rather than a
    sample of something larger, which keeps the result inside the observed range.
    """
    if not values:
        return {"p50": None, "p95": None}
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0]}
    cuts = statistics.quantiles(values, n=20, method="inclusive")
    return {"p50": round(statistics.median(values)), "p95": round(cuts[18])}


def _routed_right(row: ResultRow) -> bool:
    return row.route in {row.category, *row.also_accept}


def _classification(answered: Sequence[ResultRow]) -> dict[str, Any]:
    routed = [row for row in answered if row.route is not None]
    by_category: dict[str, float | None] = {}
    for category in ("mihail_related", "small_talk", "out_of_scope"):
        group = [row for row in routed if row.category == category]
        by_category[category] = _share(sum(_routed_right(row) for row in group), len(group))
    return {
        "accuracy": _share(sum(_routed_right(row) for row in routed), len(routed)),
        "by_category": by_category,
    }


def _retrieval(answered: Sequence[ResultRow]) -> dict[str, Any]:
    scored = [row for row in answered if row.recall is not None]
    return {
        "cases": len(scored),
        "hit_rate": _share(sum(1 for row in scored if row.recall), len(scored)),
        "recall": _mean([row.recall for row in scored if row.recall is not None]),
        "precision": _mean([row.precision for row in scored if row.precision is not None]),
        "mrr": _mean([row.mrr for row in scored if row.mrr is not None]),
    }


def _judge(answered: Sequence[ResultRow]) -> dict[str, Any]:
    judged = [row.judge_scores for row in answered if row.judge_scores is not None]
    summary: dict[str, Any] = {"cases": len(judged)}
    for dimension in JUDGED:
        scores = [scores[dimension] for scores in judged if scores.get(dimension) is not None]
        summary[dimension] = _mean(scores)
    return summary


def _fallback(answered: Sequence[ResultRow], rules: Counter[str]) -> dict[str, Any]:
    expected = [row for row in answered if row.expect_fallback]
    # Where the judge read a knowledge-base answer, does the phrase match behind
    # fallback_used agree with it about whether the answer declined? This is the
    # measurement postprocess.is_fallback asks for.
    pairs = [
        (row.fallback_used, row.judge_scores.get("declined"))
        for row in answered
        if row.route == "mihail_related" and row.judge_scores is not None
    ]
    return {
        "expected": len(expected),
        "declined_when_expected": _share(
            sum("did_not_decline" not in row.violations for row in expected), len(expected)
        ),
        "declined_answerable": rules["declined_answerable"],
        "phrase_match_agrees": _share(sum(match == judged for match, judged in pairs), len(pairs)),
    }


def _reasoning_tokens(row: ResultRow) -> int | None:
    """The tokens the answering calls spent thinking, the judge's and embeddings' left out.

    Reasoning tokens are billed and never shown, and they are what a lower reasoning
    effort saves: the number an effort comparison is really about.
    """
    answering = [call for call in row.calls if call.get("step") not in {"judge", "embed"}]
    if not answering:
        return None
    return sum(int(call.get("reasoning_tokens") or 0) for call in answering)


def _timings(values: Sequence[int | None], knowledge_base: Sequence[int | None]) -> dict[str, Any]:
    return {
        "all": _percentiles([value for value in values if value is not None]),
        "mihail_related": _percentiles([value for value in knowledge_base if value is not None]),
    }


def _cost(rows: Sequence[ResultRow], answered: Sequence[ResultRow]) -> dict[str, str | None]:
    # Strings, because JSON has no decimal type and a float would bring back the
    # rounding Decimal exists to avoid.
    answer_costs = [row.cost for row in answered if row.cost is not None]
    answers = sum(answer_costs, Decimal(0))
    judge = sum((row.judge_cost for row in rows if row.judge_cost is not None), Decimal(0))
    per_answer = round(answers / len(answer_costs), 6) if answer_costs else None
    return {
        "answers": str(answers),
        "per_answer": str(per_answer) if per_answer is not None else None,
        "judge": str(judge),
    }


def totals(rows: Sequence[ResultRow]) -> Totals:
    """Everything a run is judged on, as JSON-ready values."""
    answered = [row for row in rows if row.answer is not None]
    knowledge_base = [row for row in answered if row.route == "mihail_related"]

    rules: Counter[str] = Counter()
    for row in rows:
        rules.update(row.violations.keys())

    return {
        "cases": len(rows),
        "answered": len(answered),
        "errors": {
            "answer": sum(1 for row in rows if row.answer is None),
            "judge": sum(1 for row in answered if row.error),
        },
        "classification": _classification(answered),
        "retrieval": _retrieval(answered),
        "judge": _judge(answered),
        "fallback": _fallback(answered, rules),
        "rules": dict(sorted(rules.items())),
        "cases_with_violations": sum(1 for row in rows if row.violations),
        "latency_ms": _timings(
            [row.latency_ms for row in answered], [row.latency_ms for row in knowledge_base]
        ),
        "first_token_ms": _timings(
            [row.first_token_ms for row in answered],
            [row.first_token_ms for row in knowledge_base],
        ),
        "tokens": {
            "prompt_mean": _mean([row.prompt_tokens for row in answered if row.prompt_tokens]),
            "completion_mean": _mean(
                [row.completion_tokens for row in answered if row.completion_tokens]
            ),
            "reasoning_mean": _mean(
                [tokens for row in answered if (tokens := _reasoning_tokens(row)) is not None]
            ),
        },
        "cost_usd": _cost(rows, answered),
    }


# --- the headline table ---------------------------------------------------------


def _get(totals: Totals, path: str) -> Any:
    value: Any = totals
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _seconds(value: Any) -> float | None:
    return value / 1000 if isinstance(value, int) else None


def _dollars(value: Any) -> float | None:
    return float(value) if isinstance(value, str) else None


def _plain(value: Any) -> Any:
    return value


# (label, path into the totals, conversion, decimals). The order is the order a
# reader wants: did it route, did it find, did it answer well, how did it behave,
# how long did it take, what did it cost.
_ROWS: tuple[tuple[str, str, Callable[[Any], Any], int], ...] = (
    ("cases answered", "answered", _plain, 0),
    ("classification", "classification.accuracy", _plain, 3),
    ("retrieval hit rate", "retrieval.hit_rate", _plain, 3),
    ("recall", "retrieval.recall", _plain, 3),
    ("precision", "retrieval.precision", _plain, 3),
    ("MRR", "retrieval.mrr", _plain, 3),
    ("faithfulness, 1-5", "judge.faithfulness", _plain, 2),
    ("completeness, 1-5", "judge.completeness", _plain, 2),
    ("style, 1-5", "judge.style", _plain, 2),
    ("declined when expected", "fallback.declined_when_expected", _plain, 3),
    ("declined answerable", "fallback.declined_answerable", _plain, 0),
    ("phrase match agrees", "fallback.phrase_match_agrees", _plain, 3),
    ("cases breaking a rule", "cases_with_violations", _plain, 0),
    ("first word p50, s", "first_token_ms.mihail_related.p50", _seconds, 1),
    ("first word p95, s", "first_token_ms.mihail_related.p95", _seconds, 1),
    ("whole answer p50, s", "latency_ms.mihail_related.p50", _seconds, 1),
    ("whole answer p95, s", "latency_ms.mihail_related.p95", _seconds, 1),
    ("reasoning tokens, mean", "tokens.reasoning_mean", _plain, 0),
    ("cost per answer, $", "cost_usd.per_answer", _dollars, 4),
    ("answers total, $", "cost_usd.answers", _dollars, 4),
    ("judge total, $", "cost_usd.judge", _dollars, 4),
)


def _cell(value: Any, decimals: int) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:.{decimals}f}"
    return str(value)


def _table(runs: Sequence[RunRecord]) -> list[list[str]]:
    """The headline figures as rows of cells: label, one per run, and the change
    when there are exactly two. Both the text and the Markdown layouts draw this."""
    rows: list[list[str]] = []

    for label, path, convert, decimals in _ROWS:
        values = [convert(_get(run.totals or {}, path)) for run in runs]
        row = [label, *(_cell(value, decimals) for value in values)]
        if len(runs) == PAIR:
            numeric = all(isinstance(value, (int, float)) for value in values)
            row.append(f"{values[1] - values[0]:+.{decimals}f}" if numeric else "")
        rows.append(row)

    rules = sorted({rule for run in runs for rule in (run.totals or {}).get("rules", {})})
    for rule in rules:
        counts = [(run.totals or {}).get("rules", {}).get(rule, 0) for run in runs]
        row = [f"rule: {rule}", *(str(count) for count in counts)]
        if len(runs) == PAIR:
            row.append(f"{counts[1] - counts[0]:+d}")
        rows.append(row)

    return rows


def _labels(runs: Sequence[RunRecord]) -> list[str]:
    return ["", *(run.label for run in runs), *(["change"] if len(runs) == PAIR else [])]


def headline(runs: Sequence[RunRecord]) -> list[str]:
    """The totals of one or more runs side by side, with the change when two."""
    return [
        f"{row[0]:<30}" + "".join(f"{cell:>16}" for cell in row[1:])
        for row in [_labels(runs), *_table(runs)]
    ]


def describe(run: RunRecord) -> list[str]:
    """What a run was: when, against what, with which configuration."""
    config = run.config
    corpus = config.get("corpus") or {}
    judge = config.get("judge") or {}
    git = config.get("git") or {}
    chat_effort = config.get("chat_effort") or "default"
    classifier_effort = config.get("classifier_effort") or "default"
    dirty = " (with uncommitted changes)" if git.get("dirty") else ""
    return [
        f"{run.label}  ({run.dataset}, {run.status}, started {run.started_at:%Y-%m-%d %H:%M})",
        f"  chat        {config.get('chat_model')}, effort {chat_effort}",
        f"  classifier  {config.get('classifier_model')}, effort {classifier_effort}",
        f"  retrieval   top_k {config.get('top_k')}, up to {config.get('max_search_rounds')} "
        f"searches, {config.get('embedding_model')}",
        f"  judge       {judge.get('model', 'none')} {judge.get('prompt', '')}".rstrip(),
        f"  corpus      {corpus.get('documents')} documents, {corpus.get('chunks')} chunks, "
        f"{corpus.get('fingerprint')}",
        f"  code        {git.get('revision', 'unknown')}{dirty}",
    ]


# --- one run, case by case ------------------------------------------------------


def _scores(row: ResultRow) -> str:
    if not row.judge_scores:
        return "  -  -  -"
    return "".join(
        f"{'-' if row.judge_scores.get(name) is None else row.judge_scores[name]:>3}"
        for name in JUDGED
    )


def _problems(row: ResultRow) -> list[str]:
    problems = list(row.violations)
    if row.error:
        problems.append("error")
    scores = row.judge_scores or {}
    problems += [
        f"low {name}"
        for name in JUDGED
        if scores.get(name) is not None and scores[name] <= LOW_SCORE
    ]
    return problems


def cases(rows: Sequence[ResultRow], *, failures_only: bool = False) -> list[str]:
    """One line per case: route, retrieval, judge scores, timings, anything wrong."""
    lines = [
        f"{'case':<32}{'route':<16}{'hit':>4}{'F':>3}{'C':>3}{'S':>3}"
        f"{'first':>8}{'total':>8}  problems"
    ]
    for row in rows:
        problems = _problems(row)
        if failures_only and not problems:
            continue
        hit = "-" if row.recall is None else ("yes" if row.recall else "no")
        first = f"{row.first_token_ms / 1000:.1f}s" if row.first_token_ms is not None else "-"
        total = f"{row.latency_ms / 1000:.1f}s" if row.latency_ms is not None else "-"
        lines.append(
            f"{row.key:<32}{row.route or '-':<16}{hit:>4}{_scores(row)}"
            f"{first:>8}{total:>8}  {', '.join(problems)}"
        )
        if failures_only:
            lines += _explain(row)
    return lines


def _explain(row: ResultRow) -> list[str]:
    """For --failures: what went wrong, in words."""
    lines = [f"    question   {row.question}"]
    if row.expected_doc_ids:
        lines.append(f"    expected   {', '.join(row.expected_doc_ids)}")
        lines.append(f"    retrieved  {', '.join(row.retrieved_doc_ids) or 'nothing'}")
    for rule, detail in row.violations.items():
        lines.append(f"    {rule:<10} {detail}")
    if row.error:
        lines.append(f"    error      {row.error}")
    if row.judge_rationale:
        lines.append(f"    judge      {row.judge_rationale}")
    if row.answer:
        answer = " ".join(row.answer.split())
        cut = "..." if len(answer) > ANSWER_PREVIEW else ""
        lines.append(f"    answer     {answer[:ANSWER_PREVIEW]}{cut}")
    lines.append("")
    return lines


# --- two runs, case by case -----------------------------------------------------


def _differences(a: ResultRow, b: ResultRow) -> tuple[list[str], list[str]]:
    """What got worse and what got better from run A to run B, for one case."""
    worse: list[str] = []
    better: list[str] = []

    def note(changed: bool, text: str, *, improved: bool) -> None:
        if changed:
            (better if improved else worse).append(text)

    note(
        _routed_right(a) != _routed_right(b),
        f"route {a.route} -> {b.route}",
        improved=_routed_right(b),
    )
    if a.recall is not None and b.recall is not None:
        note(
            bool(a.recall) != bool(b.recall),
            "found" if b.recall else "missed",
            improved=b.recall > 0,
        )

    for name in JUDGED:
        before = (a.judge_scores or {}).get(name)
        after = (b.judge_scores or {}).get(name)
        if before is not None and after is not None and abs(after - before) >= SCORE_CHANGE:
            note(True, f"{name} {before} -> {after}", improved=after > before)

    for rule in sorted(set(b.violations) - set(a.violations)):
        worse.append(f"+{rule}")
    for rule in sorted(set(a.violations) - set(b.violations)):
        better.append(f"-{rule}")

    note(
        bool(a.error) != bool(b.error),
        "error" if b.error else "no longer errors",
        improved=not b.error,
    )
    return worse, better


def compare(a_rows: Sequence[ResultRow], b_rows: Sequence[ResultRow]) -> list[str]:
    """The cases that changed between two runs of the same dataset."""
    before = {row.case_id: row for row in a_rows}
    worse: list[str] = []
    better: list[str] = []

    for row in b_rows:
        earlier = before.get(row.case_id)
        if earlier is None:
            continue
        got_worse, got_better = _differences(earlier, row)
        if got_worse:
            worse.append(f"  {row.key:<32}{'; '.join(got_worse + got_better)}")
        elif got_better:
            better.append(f"  {row.key:<32}{'; '.join(got_better)}")

    missing = len(set(before) ^ {row.case_id for row in b_rows})
    lines = [f"worse in the second run ({len(worse)})", *worse, ""]
    lines += [f"better in the second run ({len(better)})", *better]
    if missing:
        lines += ["", f"{missing} case(s) ran in only one of the two runs, and are not compared."]
    return lines


def markdown(runs: Sequence[RunRecord], changes: Sequence[str]) -> str:
    """The comparison as Markdown, for pasting into docs/EVALS.md."""
    labels = _labels(runs)
    lines = ["| " + " | ".join(labels) + " |", "|" + "---|" * len(labels)]
    lines += ["| " + " | ".join(row) + " |" for row in _table(runs)]
    return "\n".join([*lines, "", "```", *changes, "```", ""])
