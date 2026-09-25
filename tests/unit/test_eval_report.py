"""A run's totals, and how two runs are compared: arithmetic on rows, no database."""

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

from portfolio_ai.db.evals import ResultRow, RunRecord
from portfolio_ai.evals import report


def _row(**overrides: Any) -> ResultRow:
    values: dict[str, Any] = {
        "case_id": 1,
        "key": "job-title",
        "question": "What is Mihail's job title?",
        "category": "mihail_related",
        "also_accept": [],
        "expected_doc_ids": ["about-mihail"],
        "expect_fallback": False,
        "answer": "He is a Solutions Architect.",
        "route": "mihail_related",
        "retrieved_doc_ids": ["about-mihail"],
        "recall": 1.0,
        "precision": 1.0,
        "mrr": 1.0,
        "judge_scores": {"faithfulness": 5, "completeness": 5, "style": 5, "declined": False},
        "judge_rationale": "Fine.",
        "violations": {},
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cost": Decimal("0.004"),
        "judge_cost": Decimal("0.02"),
        "latency_ms": 10_000,
        "first_token_ms": 8_000,
        "top_score": 0.7,
        "fallback_used": False,
        "calls": [],
        "error": None,
        **overrides,
    }
    return ResultRow(**values)


def _run(label: str, rows: list[ResultRow]) -> RunRecord:
    return RunRecord(
        id=1,
        label=label,
        dataset_id=1,
        dataset="golden_test",
        status="complete",
        config={},
        totals=report.totals(rows),
        started_at=dt.datetime(2026, 9, 23, tzinfo=dt.UTC),
        finished_at=None,
    )


def test_classification_counts_a_route_the_case_also_accepts_as_right() -> None:
    rows = [
        _row(),
        _row(case_id=2, category="small_talk", route="mihail_related", also_accept=[]),
        _row(
            case_id=3, category="small_talk", route="mihail_related", also_accept=["mihail_related"]
        ),
    ]

    classification = report.totals(rows)["classification"]

    assert classification["accuracy"] == round(2 / 3, 3)
    assert classification["by_category"] == {
        "mihail_related": 1.0,
        "small_talk": 0.5,
        "out_of_scope": None,
    }


def test_retrieval_is_averaged_over_the_cases_that_expect_documents() -> None:
    rows = [
        _row(recall=1.0, precision=0.5, mrr=1.0),
        _row(case_id=2, recall=0.0, precision=0.0, mrr=0.0),
        _row(
            case_id=3,
            category="small_talk",
            route="small_talk",
            recall=None,
            precision=None,
            mrr=None,
        ),
    ]

    retrieval = report.totals(rows)["retrieval"]

    assert retrieval == {
        "cases": 2,
        "hit_rate": 0.5,
        "recall": 0.5,
        "precision": 0.25,
        "mrr": 0.5,
    }


def test_judge_means_leave_out_what_was_not_applicable() -> None:
    rows = [
        _row(judge_scores={"faithfulness": 4, "completeness": 2, "style": 5, "declined": False}),
        _row(
            case_id=2,
            judge_scores={"faithfulness": None, "completeness": None, "style": 3, "declined": True},
        ),
        _row(case_id=3, judge_scores=None),
    ]

    assert report.totals(rows)["judge"] == {
        "cases": 2,
        "faithfulness": 4.0,
        "completeness": 2.0,
        "style": 4.0,
    }


def test_the_phrase_match_is_measured_against_the_judge() -> None:
    """postprocess.is_fallback asks the evals to say how often it is wrong."""
    judged = {"faithfulness": None, "completeness": None, "style": 4}
    rows = [
        _row(fallback_used=True, judge_scores={**judged, "declined": True}),
        # A decline in other words: the phrase match missed it.
        _row(case_id=2, fallback_used=False, judge_scores={**judged, "declined": True}),
        _row(case_id=3, fallback_used=False, judge_scores={**judged, "declined": False}),
        _row(case_id=4, fallback_used=False, judge_scores={**judged, "declined": False}),
    ]

    assert report.totals(rows)["fallback"]["phrase_match_agrees"] == pytest.approx(0.75)


def test_fallback_cases_and_rules_are_counted() -> None:
    rows = [
        _row(expect_fallback=True, expected_doc_ids=[], recall=None, violations={}),
        _row(case_id=2, expect_fallback=True, violations={"did_not_decline": True}),
        _row(case_id=3, violations={"declined_answerable": True, "too_long": 9}),
    ]

    totals = report.totals(rows)

    assert totals["fallback"]["expected"] == 2
    assert totals["fallback"]["declined_when_expected"] == pytest.approx(0.5)
    assert totals["fallback"]["declined_answerable"] == 1
    assert totals["rules"] == {"declined_answerable": 1, "did_not_decline": 1, "too_long": 1}
    assert totals["cases_with_violations"] == 2


def test_timings_are_medians_and_95th_percentiles_of_knowledge_base_answers() -> None:
    rows = [_row(case_id=n, first_token_ms=n * 1000, latency_ms=n * 2000) for n in range(1, 21)]
    rows.append(_row(case_id=99, route="small_talk", category="small_talk", first_token_ms=100))

    totals = report.totals(rows)

    assert totals["first_token_ms"]["mihail_related"] == {"p50": 10_500, "p95": 19_050}
    assert totals["first_token_ms"]["all"]["p50"] == 10_000, "the small-talk answer counts here"
    assert totals["latency_ms"]["mihail_related"]["p50"] == 21_000


def test_one_timing_is_its_own_percentile_and_none_is_none() -> None:
    assert report.totals([_row(first_token_ms=5000)])["first_token_ms"]["all"] == {
        "p50": 5000,
        "p95": 5000,
    }
    assert report.totals([_row(first_token_ms=None)])["first_token_ms"]["all"] == {
        "p50": None,
        "p95": None,
    }


def test_reasoning_tokens_count_the_answering_calls_only() -> None:
    """What a lower effort saves. The judge thinks too, but on the grader's bill."""
    calls = [
        {"step": "classify", "reasoning_tokens": 64},
        {"step": "search", "reasoning_tokens": 300},
        {"step": "embed", "reasoning_tokens": 0},
        {"step": "answer", "reasoning_tokens": 500},
        {"step": "judge", "reasoning_tokens": 2000},
    ]
    rows = [
        _row(calls=calls),
        _row(case_id=2, calls=[{"step": "small_talk", "reasoning_tokens": 36}]),
    ]

    assert report.totals(rows)["tokens"]["reasoning_mean"] == pytest.approx(450.0)


def test_costs_are_decimal_strings_and_the_judge_is_counted_apart() -> None:
    rows = [
        _row(cost=Decimal("0.004"), judge_cost=Decimal("0.02")),
        _row(case_id=2, cost=Decimal("0.002"), judge_cost=Decimal("0.03")),
        _row(
            case_id=3, answer=None, route=None, cost=None, judge_cost=None, error="answer: failed"
        ),
    ]

    totals = report.totals(rows)

    assert totals["cost_usd"] == {"answers": "0.006", "per_answer": "0.003000", "judge": "0.05"}
    assert totals["errors"] == {"answer": 1, "judge": 0}
    assert totals["answered"] == 2


def test_a_comparison_lists_what_got_worse_and_what_got_better() -> None:
    before = [
        _row(case_id=1, key="job-title"),
        _row(case_id=2, key="python", recall=0.0, violations={"too_long": 8}),
        _row(case_id=3, key="steady"),
    ]
    after = [
        _row(
            case_id=1,
            key="job-title",
            judge_scores={"faithfulness": 2, "completeness": 5, "style": 5, "declined": False},
            violations={"invented_link": ["https://example.com"]},
        ),
        _row(case_id=2, key="python", recall=1.0),
        _row(case_id=3, key="steady"),
    ]

    lines = report.compare(before, after)

    assert lines[0] == "worse in the second run (1)"
    assert "faithfulness 5 -> 2" in lines[1]
    assert "+invented_link" in lines[1]
    assert "better in the second run (1)" in lines
    better = lines[lines.index("better in the second run (1)") + 1]
    assert "python" in better
    assert "found" in better
    assert "-too_long" in better
    assert not any("steady" in line for line in lines)


def test_two_runs_side_by_side_show_the_change() -> None:
    fast = _row(first_token_ms=6000, latency_ms=8000)
    slow = _row(first_token_ms=22000, latency_ms=26000)

    lines = report.headline([_run("baseline", [slow]), _run("effort-low", [fast])])

    assert lines[0].split() == ["baseline", "effort-low", "change"]
    first_word = next(line for line in lines if line.startswith("first word p50"))
    assert first_word.split()[-3:] == ["22.0", "6.0", "-16.0"]


def test_the_markdown_comparison_is_a_table_and_the_case_list() -> None:
    runs = [_run("baseline", [_row()]), _run("effort-low", [_row()])]

    text = report.markdown(runs, ["worse in the second run (0)"])

    assert text.startswith("|  | baseline | effort-low | change |\n|---|---|---|---|\n")
    assert "| classification | 1.000 | 1.000 | +0.000 |" in text
    assert "```\nworse in the second run (0)\n```" in text
