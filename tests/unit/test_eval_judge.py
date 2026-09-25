"""The judge: what it is shown, and what it does with a reply it cannot use.

The OpenAI API is the fake from openai_fake.py, so the real SDK builds each request
and parses each structured reply, exactly as it does for the classifier.
"""

import pytest
from openai_fake import FAKE_CLIENT_TIMEOUT_SECONDS, FakeOpenAI, install_fake_openai

from portfolio_ai.assistant.classifier import MessageCategory
from portfolio_ai.assistant.prompts.loader import JUDGE
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.evals import judge as judging
from portfolio_ai.evals.datasets import EvalCase
from portfolio_ai.llm import responses

CASE = EvalCase(
    key="python",
    question="Does Mihail know Python?",
    category="mihail_related",
    expected_doc_ids=("tech-stack",),
    reference_answer="Yes, from his RPA work, but it is not in his current stack.",
)

CHUNKS = [
    RetrievedChunk(
        chunk_id=7,
        doc_id="tech-stack",
        title="Technical Skills and Tools",
        url="https://mihaylov.io/?section=about",
        section="does-mihail-know-python",
        section_title="Does Mihail know Python?",
        content="Yes, from his enterprise RPA work at Deloitte.",
        score=0.71,
    )
]

ANSWER = "Yes. Mihail used Python in his RPA work at Deloitte, though it isn't in his stack now."


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    return install_fake_openai(monkeypatch)


async def _judge(case: EvalCase = CASE) -> judging.Judgement:
    return await judging.judge(
        case, route="mihail_related", chunks=CHUNKS, answer=ANSWER, model="gpt-5"
    )


async def test_a_verdict_comes_back_with_what_it_cost(fake: FakeOpenAI) -> None:
    fake.judge(faithfulness=5, completeness=4, style=5, rationale="Accurate and short.")

    judgement = await _judge()

    assert judgement.problem is None
    assert judgement.verdict is not None
    assert judgement.verdict.as_record() == {
        "faithfulness": 5,
        "completeness": 4,
        "style": 5,
        "declined": False,
    }
    assert judgement.verdict.rationale == "Accurate and short."
    assert judgement.usage.step == "judge"
    assert judgement.usage.prompt == JUDGE.ref
    assert judgement.usage.cost > 0


async def test_the_judge_reads_the_rubric_the_passages_the_reference_and_the_answer(
    fake: FakeOpenAI,
) -> None:
    fake.judge()

    await _judge()

    [request] = fake.requests
    assert request["model"] == "gpt-5"
    assert request["instructions"] == JUDGE.text
    brief = request["input"][0]["content"]
    assert "Question:\nDoes Mihail know Python?" in brief
    assert '[1] Technical Skills and Tools (tech-stack), "Does Mihail know Python?"' in brief
    assert "Reference answer:\nYes, from his RPA work" in brief
    assert f"Rachel's answer:\n{ANSWER}" in brief


async def test_the_judge_waits_longer_than_a_visitor_facing_call(fake: FakeOpenAI) -> None:
    # The first baseline lost three verdicts to the 30-second client timeout.
    fake.judge(faithfulness=5, completeness=5, style=5, rationale="Fine.")
    fake.classify("small_talk")

    await _judge()
    await responses.parse(
        step="classifier",
        model="gpt-5-mini",
        instructions="Classify.",
        input=[{"role": "user", "content": "hi"}],
        text_format=MessageCategory,
    )

    # The second call keeps the client's own timeout: the override belongs to the
    # judge's call alone. Asserting the value, not just "not 180", also catches
    # not_given swapped for None, which would mean no timeout on any call.
    judge_timeout, other_timeout = fake.read_timeouts
    assert judge_timeout == judging.JUDGE_TIMEOUT_SECONDS
    assert other_timeout == FAKE_CLIENT_TIMEOUT_SECONDS


async def test_a_reply_that_does_not_fit_the_schema_is_no_verdict_not_an_error(
    fake: FakeOpenAI,
) -> None:
    fake.unparseable()

    judgement = await _judge()

    assert judgement.verdict is None
    assert judgement.problem == "the judge's reply could not be read"


async def test_a_score_outside_the_rubric_is_refused(fake: FakeOpenAI) -> None:
    """Averaging a 7 into a 1-5 scale would quietly inflate the run."""
    fake.judge(style=7)

    judgement = await _judge()

    assert judgement.verdict is None
    assert judgement.problem is not None
    assert "outside 1-5" in judgement.problem


async def test_not_applicable_is_a_valid_score(fake: FakeOpenAI) -> None:
    fake.judge(faithfulness=None, completeness=None, declined=True)

    judgement = await _judge()

    assert judgement.verdict is not None
    assert judgement.verdict.as_record()["faithfulness"] is None
    assert judgement.verdict.declined


def test_the_brief_shows_the_conversation_and_says_when_nothing_was_found() -> None:
    # model_validate rather than the constructor: it takes plain data, which pydantic
    # turns into Turn objects, where the constructor's types would expect them made.
    case = EvalCase.model_validate(
        {
            "key": "followup",
            "question": "yes please",
            "category": "mihail_related",
            "history": [
                {"role": "user", "content": "What is Glotsmith?"},
                {"role": "assistant", "content": "A language notebook. More detail?"},
            ],
        }
    )

    brief = judging.brief(case, route="mihail_related", chunks=[], answer="Here it is.")

    assert "Visitor: What is Glotsmith?\nRachel: A language notebook. More detail?" in brief
    assert "Passages Rachel was shown: none" in brief
    assert "Reference answer:\nnone" in brief
