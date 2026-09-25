"""A second model's opinion of an answer: the part of an eval no pattern can check.

Whether every claim in an answer is backed by what retrieval found, whether it
covers what a person decided a good answer covers, whether it sounds like Rachel --
those take reading. So a stronger model reads them, with a written rubric
(``prompts/judge.md``), and returns scores in a fixed shape.

Three rules keep the judge honest, and all three come from ARCHITECTURE.md §9:

- **It is a separate call.** An answer is never asked to grade itself.
- **Its model is pinned apart from the one under test** (``JUDGE_MODEL``), so trying
  a new chat model changes what is measured, never the ruler.
- **Its prompt is versioned like Rachel's.** A change to how answers are graded moves
  every score, so it is recorded with every run and pinned by test_prompts.py.

The judge is shown the passages the model was shown, not the knowledge base. It
grades what the answer did with what it had; whether retrieval found the right
passages is scored separately, in metrics.py.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.evals.datasets import EvalCase
from portfolio_ai.llm import responses
from portfolio_ai.llm.responses import CallUsage

SCORES = range(1, 6)

# OPENAI_TIMEOUT_SECONDS (30) is sized for a visitor waiting on an answer. The judge
# reads twenty-odd passages at gpt-5's default effort, and on the longest answers
# took longer than that on every retry: three of the first baseline's 54 cases went
# ungraded. Nobody is waiting on a judge, so it gets room.
JUDGE_TIMEOUT_SECONDS = 180.0


# The shape the judge must answer in. Like the classifier's, it has no docstring on
# purpose: the class's docstring would become the JSON schema's description, which
# the model reads, and the rubric already lives in judge.md.
class Verdict(BaseModel):
    faithfulness: int | None
    completeness: int | None
    style: int
    declined: bool
    rationale: str

    def as_record(self) -> dict[str, object]:
        """The scores as stored in ``eval_results.judge_scores``; the rationale has
        a column of its own."""
        return {
            "faithfulness": self.faithfulness,
            "completeness": self.completeness,
            "style": self.style,
            "declined": self.declined,
        }


@dataclass(frozen=True)
class Judgement:
    """A verdict, or the reason there is none, and what asking cost either way."""

    verdict: Verdict | None
    usage: CallUsage
    problem: str | None = None


def brief(case: EvalCase, *, route: str, chunks: Sequence[RetrievedChunk], answer: str) -> str:
    """Everything the judge needs, as one plain-text message.

    Plain text with headings rather than JSON: the judge reads it, and passages full
    of quotes and newlines are far easier to read unescaped.
    """
    parts = [f"Route: {route}"]

    if case.history:
        speaker = {"user": "Visitor", "assistant": "Rachel"}
        lines = [f"{speaker[turn.role]}: {turn.content}" for turn in case.history]
        parts.append("Earlier conversation:\n" + "\n".join(lines))

    parts.append(f"Question:\n{case.question}")

    if chunks:
        passages = [
            f"[{number}] {chunk.title} ({chunk.doc_id}), "
            f'"{chunk.section_title or chunk.section or "main"}"\n{chunk.content}'
            for number, chunk in enumerate(chunks, start=1)
        ]
        parts.append(f"Passages Rachel was shown ({len(chunks)}):\n\n" + "\n\n".join(passages))
    else:
        parts.append("Passages Rachel was shown: none")

    parts.append(f"Reference answer:\n{case.reference_answer or 'none'}")
    parts.append(f"Rachel's answer:\n{answer}")
    return "\n\n".join(parts)


async def judge(
    case: EvalCase,
    *,
    route: str,
    chunks: Sequence[RetrievedChunk],
    answer: str,
    model: str,
) -> Judgement:
    """Grade one answer. Returns without a verdict, rather than raising, when the
    judge's reply is unusable; an OpenAI failure still raises, as every call does."""
    result = await responses.parse(
        step="judge",
        model=model,
        instructions=JUDGE.text,
        input=[{"role": "user", "content": brief(case, route=route, chunks=chunks, answer=answer)}],
        text_format=Verdict,
        prompt=JUDGE.ref,
        request_timeout=JUDGE_TIMEOUT_SECONDS,
    )

    verdict = result.value
    if verdict is None:
        return Judgement(None, result.usage, "the judge's reply could not be read")

    # The schema says "integer"; the rubric says 1 to 5. A score outside that is a
    # judge not following its own instructions, and averaging a 7 in would quietly
    # inflate a run.
    scores = (verdict.faithfulness, verdict.completeness, verdict.style)
    if any(score is not None and score not in SCORES for score in scores):
        return Judgement(None, result.usage, f"the judge scored outside 1-5: {scores}")

    return Judgement(verdict, result.usage)
