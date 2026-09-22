"""The ``search_knowledgebase`` tool: embed a query, find the closest chunks.

This is what runs when the model calls the tool. The model writes the query itself
-- that is the query rewriting ARCHITECTURE.md describes: the tool's description
(``prompts/search_tool.md``) tells it to strip filler and the name "Mihail" and to
search in keywords. So "Does Mihail work with Laravel?" arrives here as something
like "Laravel PHP experience".

What goes back to the model is shaped the way n8n shaped it -- each chunk's content
with its document's title and URL -- and, like n8n, without the similarity scores.
The prompts were tuned against that shape, and a number next to every chunk invites
the model to reason about the number rather than the content.
"""

import json
import time
from collections.abc import Collection
from dataclasses import dataclass

from openai import APIError
from openai.types.responses import FunctionToolParam

from portfolio_ai.assistant.prompts.loader import SEARCH_TOOL
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.llm.embeddings import embed_texts
from portfolio_ai.llm.responses import CallUsage, domain_error

TOOL_NAME = "search_knowledgebase"

# The tool as the model sees it -- all of it, including the parameter's description,
# which is why that is n8n's wording too. The one change is the parameter's name:
# `query` rather than n8n's `input`, which matches the description's own language
# ("How to form the search query").
#
# strict=True makes the API hold the model to this schema exactly: the arguments
# always parse, and always contain a query. Strict mode requires every property to
# be listed in `required` and extra properties to be forbidden, which is why both
# appear even though there is only one property.
TOOL: FunctionToolParam = {
    "type": "function",
    "name": TOOL_NAME,
    "description": SEARCH_TOOL.text,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query to search for. Required"},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    "strict": True,
}


@dataclass(frozen=True)
class SearchResult:
    """One run of the tool."""

    query: str
    # What the model is shown: the chunks this search found that no earlier search
    # in the same answer already returned, best first.
    chunks: list[RetrievedChunk]
    # How many chunks the search returned before the already-seen ones were dropped.
    hits: int
    # The best score among everything the search returned, seen or not. This is the
    # content-gap signal: a question whose best match scores low is a question the
    # knowledge base does not answer.
    top_score: float | None
    duration_ms: int
    embedding: CallUsage

    def for_model(self) -> str:
        """The tool output, as the text the model reads.

        ``ensure_ascii=False`` keeps curly quotes and dashes as themselves. The
        default escapes them as ``\\u2019``, which is six characters of noise in
        place of one, and the model pays for every one of them in tokens.
        """
        if not self.chunks and self.hits:
            # Everything matched was already returned by an earlier search in this
            # answer. Saying so stops an empty list being read as "no information",
            # which would turn a perfectly answerable question into a fallback.
            return "No new results: everything this search matched was returned above."

        return json.dumps(
            [
                {
                    "title": chunk.title,
                    "section": chunk.section_title,
                    "url": chunk.url,
                    "content": chunk.content,
                }
                for chunk in self.chunks
            ],
            ensure_ascii=False,
        )


async def search_knowledgebase(
    query: str, *, top_k: int, seen: Collection[int] = ()
) -> SearchResult:
    """Embed ``query``, fetch the ``top_k`` closest chunks, drop any in ``seen``."""
    started = time.perf_counter()

    try:
        embedding = await embed_texts([query])
    except APIError as exc:
        raise domain_error(exc) from exc

    found = await docs_db.search_chunks(embedding.vectors[0], top_k=top_k)

    return SearchResult(
        query=query,
        chunks=[chunk for chunk in found if chunk.chunk_id not in seen],
        hits=len(found),
        top_score=max((chunk.score for chunk in found), default=None),
        duration_ms=int((time.perf_counter() - started) * 1000),
        embedding=CallUsage(
            step="embed",
            model=embedding.model,
            prompt=None,
            input_tokens=embedding.total_tokens,
            cached_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            latency_ms=embedding.duration_ms,
            cost=embedding.cost,
        ),
    )
