"""A stand-in for api.openai.com, for tests of anything that calls a model.

The same idea as ``FakeRepo`` in test_pipeline.py: the real SDK runs unchanged, and
only the network underneath it is replaced, by a ``MockTransport``. So every request
is built by the real SDK and every response is parsed by it -- including the
server-sent event stream -- which is exactly where an integration with the Responses
API breaks when it breaks.

It is ``httpx2``'s MockTransport, not ``httpx``'s. Since version 3 the OpenAI SDK
runs on ``httpx2``, a separate package, and a fake that sits underneath the SDK has
to speak whatever the SDK speaks. ``httpx2`` arrives as the SDK's own dependency and
is deliberately not declared in pyproject.toml: this file is coupled to the SDK's
transport by its nature, and if the SDK ever moves again, this file moves with it.

A test scripts the replies in the order the calls will happen::

    fake.classify("mihail_related")
    fake.search("Laravel PHP experience")
    fake.say("Yes, Mihail works with Laravel.")

and afterwards reads ``fake.requests`` to check what was sent: which instructions,
what input, which ``tool_choice``.

Importable from both test suites because pyproject.toml puts ``tests/`` on pytest's
``pythonpath``.
"""

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from portfolio_ai.llm import embeddings, responses

# What the API reports back, as opposed to what was asked for -- the real service
# answers with the dated snapshot, and the pricing table has to cope with that.
SNAPSHOT = "gpt-5-mini-2025-08-07"
DIMENSIONS = 1536


@dataclass
class Usage:
    input_tokens: int = 100
    cached_tokens: int = 0
    output_tokens: int = 20
    reasoning_tokens: int = 8

    def as_json(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "input_tokens_details": {"cached_tokens": self.cached_tokens},
            "output_tokens": self.output_tokens,
            "output_tokens_details": {"reasoning_tokens": self.reasoning_tokens},
            "total_tokens": self.input_tokens + self.output_tokens,
        }


@dataclass
class _Reply:
    kind: str  # "structured", "text", "tools", "failed", "status"
    text: str = ""
    queries: tuple[str, ...] = ()
    status: int = 200
    usage: Usage = field(default_factory=Usage)


def _response(
    output: list[dict[str, Any]], usage: Usage, status: str = "completed"
) -> dict[str, Any]:
    return {
        "id": "resp_fake",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": SNAPSHOT,
        "output": output,
        "usage": usage.as_json(),
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "error": None,
        "incomplete_details": None,
    }


def _message(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_fake",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _sse(events: list[dict[str, Any]]) -> bytes:
    # The wire format of server-sent events: an event line, a data line, a blank
    # line. The SDK parses this exactly as it parses the real stream.
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def _pieces(text: str) -> list[str]:
    """Split text the way a model streams it: a few characters at a time."""
    return [text[i : i + 5] for i in range(0, len(text), 5)] or [""]


class FakeOpenAI:
    """Scripted replies for /v1/responses, computed ones for /v1/embeddings."""

    def __init__(self) -> None:
        self._replies: deque[_Reply] = deque()
        # Every /v1/responses request body, in order.
        self.requests: list[dict[str, Any]] = []
        # Every text sent to /v1/embeddings.
        self.embedded: list[str] = []
        # Tests that care which chunks a query finds set vectors here.
        self.vectors: dict[str, list[float]] = {}

    # --- scripting ----------------------------------------------------------

    def classify(self, label: str, *, usage: Usage | None = None) -> None:
        self._replies.append(
            _Reply("structured", text=json.dumps({"category": label}), usage=usage or Usage())
        )

    def unparseable(self) -> None:
        """A structured reply that does not match the schema."""
        self._replies.append(_Reply("structured", text='{"category": "banana"}'))

    def say(self, text: str, *, usage: Usage | None = None) -> None:
        self._replies.append(_Reply("text", text=text, usage=usage or Usage()))

    def search(self, *queries: str, preamble: str = "") -> None:
        """A reply that calls search_knowledgebase once per query, in parallel."""
        self._replies.append(_Reply("tools", text=preamble, queries=queries))

    def fail(self) -> None:
        """A stream that ends in response.failed."""
        self._replies.append(_Reply("failed"))

    def http_error(self, status: int) -> None:
        self._replies.append(_Reply("status", status=status))

    @property
    def pending(self) -> int:
        """Replies scripted but not yet asked for. A test that ends with some left
        over expected more calls than happened."""
        return len(self._replies)

    # --- serving ------------------------------------------------------------

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)

        if request.url.path.endswith("/embeddings"):
            return self._embeddings(body)

        self.requests.append(body)
        assert self._replies, f"unexpected call #{len(self.requests)}: nothing scripted for it"
        reply = self._replies.popleft()

        if reply.kind == "status":
            return httpx2.Response(reply.status, json={"error": {"message": "scripted failure"}})

        if reply.kind == "structured":
            assert not body.get("stream"), "a structured reply was scripted for a streamed call"
            return httpx2.Response(200, json=_response([_message(reply.text)], reply.usage))

        assert body.get("stream"), "a streamed reply was scripted for a non-streamed call"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(self._stream_events(reply)),
        )

    def _stream_events(self, reply: _Reply) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = [
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": _response([], reply.usage, "in_progress"),
            }
        ]

        if reply.kind == "failed":
            failed = _response([], reply.usage, "failed")
            failed["error"] = {"code": "server_error", "message": "scripted failure"}
            events.append({"type": "response.failed", "sequence_number": 1, "response": failed})
            return events

        events += [
            {
                "type": "response.output_text.delta",
                "sequence_number": i + 1,
                "item_id": "msg_fake",
                "output_index": 0,
                "content_index": 0,
                "delta": piece,
                "logprobs": [],
            }
            for i, piece in enumerate(_pieces(reply.text))
            if piece
        ]

        output: list[dict[str, Any]] = [
            # A reasoning item, encrypted as the real one is with store=False. It has to
            # travel back with the next request, and the tests check that it does.
            {"type": "reasoning", "id": "rs_fake", "summary": [], "encrypted_content": "sealed"}
        ]
        if reply.text:
            output.append(_message(reply.text))
        output += [
            {
                "type": "function_call",
                "id": f"fc_{i}",
                "call_id": f"call_{len(self.requests)}_{i}",
                "name": "search_knowledgebase",
                "arguments": json.dumps({"query": query}),
                "status": "completed",
            }
            for i, query in enumerate(reply.queries)
        ]

        events.append(
            {
                "type": "response.completed",
                "sequence_number": len(events),
                "response": _response(output, reply.usage),
            }
        )
        return events

    def _embeddings(self, body: dict[str, Any]) -> httpx2.Response:
        texts = body["input"]
        self.embedded += texts
        default = [1.0] + [0.0] * (DIMENSIONS - 1)
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        "embedding": self.vectors.get(text, default),
                    }
                    for i, text in enumerate(texts)
                ],
                "usage": {"prompt_tokens": 5 * len(texts), "total_tokens": 5 * len(texts)},
            },
        )


def install_fake_openai(monkeypatch: pytest.MonkeyPatch) -> FakeOpenAI:
    """Route every OpenAI call in the process to a new :class:`FakeOpenAI`.

    Patched where the client is fetched -- ``responses.get_client`` and
    ``embeddings.get_client`` -- exactly as test_pipeline.py patches
    ``github.build_client``. ``max_retries=0``, so a scripted failure fails at once
    instead of being retried into a different scripted reply.
    """
    fake = FakeOpenAI()
    client = AsyncOpenAI(
        api_key="not-a-real-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler)),
    )
    monkeypatch.setattr(responses, "get_client", lambda: client)
    monkeypatch.setattr(embeddings, "get_client", lambda: client)
    return fake
