"""The similarity search, against a real pgvector index.

The one part of retrieval that cannot be tested without a database: whether the
query reaches the index, orders by the operator the index was built for, and turns
distance back into the similarity everything downstream reports.
"""

import corpus
import pytest

from portfolio_ai.db import documents as docs_db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _clean() -> None:
    await corpus.clear()


async def test_chunks_come_back_closest_first_with_a_similarity_score() -> None:
    await corpus.seed(
        "tech-stack",
        "Tech Stack",
        [
            ("Does Mihail use Laravel?", "Yes, it is his main PHP framework.", corpus.axis(0)),
            ("What about Vue?", "Vue and Nuxt on the front end.", corpus.between(0, 1, 0.6)),
            ("Anything else?", "Python, more recently.", corpus.axis(1)),
        ],
    )

    found = await docs_db.search_chunks(corpus.axis(0), top_k=3)

    assert [chunk.section_title for chunk in found] == [
        "Does Mihail use Laravel?",
        "What about Vue?",
        "Anything else?",
    ]
    assert found[0].score == pytest.approx(1.0)
    assert found[1].score == pytest.approx(0.8)
    assert found[2].score == pytest.approx(0.0, abs=1e-6)


async def test_the_document_travels_with_the_chunk() -> None:
    await corpus.seed(
        "hiring",
        "Hiring Mihail",
        [("Is he available?", "For selected freelance work.", corpus.axis(2))],
        url="https://mihaylov.io/#contact",
    )

    found = await docs_db.search_chunks(corpus.axis(2), top_k=1)

    assert (found[0].doc_id, found[0].title, found[0].url) == (
        "hiring",
        "Hiring Mihail",
        "https://mihaylov.io/#contact",
    )
    assert found[0].section == "section-0"
    assert "freelance" in found[0].content


async def test_top_k_limits_what_comes_back() -> None:
    await corpus.seed(
        "faq",
        "FAQ",
        [(f"Question {i}?", f"Answer {i}.", corpus.between(0, i + 1, i / 10)) for i in range(5)],
    )

    assert len(await docs_db.search_chunks(corpus.axis(0), top_k=2)) == 2


async def test_an_empty_knowledge_base_returns_nothing_rather_than_failing() -> None:
    assert await docs_db.search_chunks(corpus.axis(0), top_k=20) == []
