# The API, end to end

How a message from the site becomes a streamed answer over HTTP: what happens during one request,
which module does which part, what each library is used for, and the contract the portfolio's
Express API has to meet when it starts calling this in build step 6. The assistant underneath is
described in [ASSISTANT.md](ASSISTANT.md), and the reasoning behind the design in
[ARCHITECTURE.md](../ARCHITECTURE.md) §8. This document is about the code.

The API is private. Nothing outside the Docker network can reach it, and the only caller it
expects is the portfolio's Express API, which holds the bearer key server-side. The browser talks
to Express; Express talks to this.

## The idea it rests on

**A turn outlives the request that asked for it.**

The obvious way to stream an answer is to run the assistant inside the request: the endpoint
iterates `conversation.chat()` and forwards each event. That works until a visitor closes the tab.
Then the server cancels the request, the cancellation lands inside the assistant, and the turn
dies half-written, after the classifier and the search have been paid for and before anything was
stored. The question vanishes from the analytics, and what it cost is never counted against the
daily spending limit. That limit is the one thing meant to bound the bill whatever a caller does,
so a script that hangs up after a second would be spending without being counted.

So every turn runs in an asyncio task of its own, and the request only watches it:

```
request  --starts-->  task: conversation.chat()  --each event-->  queue
request  <--reads---  queue
```

When the visitor leaves, the server cancels the request, which cancels only the reading. The task
carries on, writes the answer nobody is waiting for, stores it and counts it. The unread rest of an
answer costs a few tenths of a cent; in return, every started turn is recorded.

Here is what that looked like on a real run against the development database, on 2026-09-23. The
client hung up after 3 seconds, having received only the first event:

```
event: route
data: {"classification":"mihail_related"}

curl exit code: 28 (28 = timed out, i.e. we hung up)
```

The server's log shows the request ending at 3,008 ms and, four seconds later, the turn finishing
with the same request id. The answer was stored with its cost:

```
{"method": "POST", "path": "/v1/chat/stream", "status": 200, "duration_ms": 3008, "event": "request", "request_id": "47263bf7...", ...}
{"classification": "mihail_related", ... "cost_usd": "0.00268081", "latency_ms": 7009, "first_token_ms": 5004, "event": "turn_answered", "request_id": "47263bf7...", ...}
```

Three asyncio details make this work, and each is easy to get wrong. All three are written up in
`api/turns.py`.

- **A task needs a reference.** The event loop keeps only a weak reference to a task, so a task
  created and forgotten can be garbage-collected mid-flight. The `Turns` registry holds every
  running turn until it finishes.
- **The request never awaits the task.** Awaiting a task and then being cancelled cancels the task
  too; that is how `await` passes cancellation down. The request reads from the queue instead, and
  cancelling a `queue.get()` touches nothing else.
- **A task copies the context it was created in.** Anything bound with structlog's
  `bind_contextvars` (here, the request id) is on every line the turn logs, even after the request
  that started it has finished. That is how the two log lines above share an id.

If you know Node: a Promise keeps running when nobody awaits it, and there is nothing to cancel.
Python's tasks can be cancelled, and that is the default fate of anything tied to a request that
goes away. Detaching work from the request here is a deliberate step.

## One request, start to finish

Follow "Tell me about Threadline" through `POST /v1/chat/stream`. The timings are from the same
real run, at the default reasoning effort.

**Middleware.** `RequestLogMiddleware` binds a fresh request id into structlog's contextvars, then
passes the request on. Everything logged while serving it carries the id. The same id goes back to
the caller in an `X-Request-ID` header, on every response, a 500 included.

**Auth.** `require_api_key` is the first dependency of every `/v1` route, because it is a
dependency of the router itself. It compares the bearer token with `PORTFOLIO_AI_API_KEY` using
`secrets.compare_digest`, on bytes. A missing or wrong key is a 401 before any of the route's own
checks run. One thing happens earlier still: FastAPI reads the whole body and parses the JSON
before any dependency runs, so a body that is not JSON at all is a 422 even without a key.

**Admission.** `deps.admit_turn` is a FastAPI dependency that takes the request body. FastAPI
validates the body against `ChatRequest` before the function runs. An empty message, one over 2,000
characters, one containing a control character, or a malformed `session_id` is a 422, and nothing
has started. Then admission makes two database reads: the conversation's questions in the last 15
minutes (`recent_questions`) and what visitors spent in the last 24 hours (`spend_since`). A
database that is down fails here, as an ordinary 503.

Then, with no `await` in between, the checks run in order:

| Check | Result |
|---|---|
| the server is shutting down | 503, `Retry-After: 5` |
| this conversation already has a turn in flight | 409 |
| 20 questions in the conversation's last 15 minutes | 429, `Retry-After` = when the oldest leaves the window |
| the last 24 hours' spend has reached `DAILY_SPEND_CAP_USD` | the fixed refusal, delivered as an answer |
| `MAX_CONCURRENT_TURNS` answers are already in flight | 503, `Retry-After: 5` |

None of these awaits, so no other request can run between the first check and the start. asyncio
switches between tasks only at an `await`. That is why two first messages for the same conversation
arriving together cannot both pass the 409 check, with no lock anywhere.

**Start.** `turns.start()` creates the task running `conversation.chat(...)` and returns a `Turn`, a
handle on the queue. The dependency returns it, FastAPI hands it to the endpoint, and the endpoint's
body starts. From here the status line is 200.

**Stream.** `chat_stream` reads the turn's events from the queue and yields each one as a
`ServerSentEvent`. FastAPI writes the SSE format, flushing each event as it is yielded:

```
  1.98s  event: route      data: {"classification":"mihail_related"}
  4.91s  event: search     data: {}
  6.47s  event: token      data: {"text":"Threadline "}
  6.48s  event: token      data: {"text":"is "}
  ...                      (one event per word, as the link filter releases them)
  8.09s  event: token      data: {"text":"architecture."}
  8.11s  event: done       data: {"message_id":8,"classification":"mihail_related","citations":[...],"usage":{...}}
```

The route is known after the classifier's call (2 s). The search event arrives when the first model
call has chosen its query and the search has run (4.9 s). The first word comes out of the second
model call (6.5 s). Tokens arrive a word at a time rather than as OpenAI's fragments, because the
link filter holds back the word in progress until it knows it is not a forbidden URL.

**Done.** After the last token, `conversation.chat()` stores the turn and yields `Done` with the
stored answer's id, and the stream ends with `done`. That `message_id` is what the browser posts
feedback against. The middleware logs one line for the request: the route that matched, the status
and the duration. It never logs the body, nor the path as it was sent. The route is the one the code
declares, such as `/v1/messages/{message_id}/feedback`, because a path holds whatever the caller put
in it.

**If the visitor leaves**, only the reading stops, as shown in the previous section.

## The contract

### Endpoints

| Method and path | Auth | What it does |
|---|---|---|
| `POST /v1/chat` | bearer | answers a message, returns the whole answer as JSON |
| `POST /v1/chat/stream` | bearer | the same answer as server-sent events |
| `POST /v1/messages/{message_id}/feedback` | bearer | a thumbs up or down on an answer |
| `GET /healthz` | none | the process is up |
| `GET /readyz` | none | the database answers (503 if not) |
| `GET /docs` | none | the OpenAPI schema, browsable |

### Requests

```jsonc
// POST /v1/chat and /v1/chat/stream
{ "session_id": "3f0c9a52-8d1e-4b7a-9c2f-6e5d4c3b2a10", "message": "Does Mihail work with Laravel?" }

// POST /v1/messages/4812/feedback
{ "session_id": "3f0c9a52-8d1e-4b7a-9c2f-6e5d4c3b2a10", "rating": -1, "comment": "Not what I asked." }
```

- `session_id`: 8 to 100 characters from `A-Z a-z 0-9 _ -`. The browser mints it with
  `crypto.randomUUID()`.
- `message`: 1 to 2,000 characters after trimming, with no control characters apart from tab,
  newline and carriage return. A NUL in particular is refused: Postgres text cannot hold one, so the
  turn would run, be paid for, and then fail to store. Neither limit would ever count it. Characters
  are code points, as Python's `len` counts them, and trimming is pydantic-core's, which strips
  Unicode `White_Space`: `U+0085` goes and `U+FEFF` stays, the reverse of JavaScript's `trim()`. The
  proxy checks the same way, character for character.
- `rating`: `1` or `-1`, and not `true`. In Python `True == 1`, so a plain `Literal[-1, 1]` accepts
  `true`, which is refused here explicitly.
- `comment`: optional, at most 1,000 characters. An empty one is stored as no comment.
- Unknown fields are a 422 (`extra="forbid"`). The only caller is our own proxy, so a misspelt field
  should fail loudly in development. The proxy refuses them itself, with a 400, rather than
  dropping them, for the same reason.

Where the visitor came from travels in headers the proxy sets, which are trusted because only the
key holder can send them:

| Header | Stored as |
|---|---|
| `X-Visitor-IP` | HMAC-SHA256 keyed with `IP_HASH_SALT`, in `chat_sessions.client_ip_hash`; IPv4 sent as `::ffff:a.b.c.d` hashes the same as `a.b.c.d` |
| `X-Visitor-User-Agent` | trimmed, cut to 512 characters |
| `X-Visitor-Referrer` | the page only: query string and fragment removed |

All three are optional and are written only when a session is first seen, so a conversation
records where it started.

### Responses

`POST /v1/chat` returns:

```json
{
  "reply": "Yes — he works with Laravel as part of his backend toolkit. ...",
  "session_id": "3f0c9a52-8d1e-4b7a-9c2f-6e5d4c3b2a10",
  "message_id": 6,
  "classification": "mihail_related",
  "citations": [{"doc_id": "project-threadline", "title": "Threadline", "url": "https://mihaylov.io/?section=projects", "section": "technology-stack"}],
  "usage": {"model": "gpt-5-mini-2025-08-07", "prompt_tokens": 7089, "completion_tokens": 312, "latency_ms": 14638, "first_token_ms": 10097}
}
```

Cost is deliberately absent; what an answer costs is not the visitor's business. When the daily
spending limit has been reached, the reply is the fixed text from `prompts/daily_limit_reply.md` and
`message_id`, `classification` and `usage` are `null`. Nothing was stored, so there is nothing to
rate, and a chat UI should hide the thumbs.

`POST /v1/chat/stream` sends these events, in this order:

| Event | Data | When |
|---|---|---|
| `route` | `{"classification": ...}` | once, first; not sent for the daily-limit refusal |
| `search` | `{}` | once per search. Empty on purpose: the query is written by the model and never passes the link filter |
| `token` | `{"text": ...}` | many times, whole words |
| `done` | `{"message_id", "classification", "citations", "usage"}` | once, last, on success |
| `error` | `{"detail", "status"}` | once, last, if the answer failed after the stream began |

Every stream ends with exactly one of `done` or `error`. A refusal streams as one `token` and a
`done` with a null `message_id`. During silences longer than 15 seconds, FastAPI also sends
`: ping` comment lines, which clients ignore and proxies take as a sign of life. The response carries
`Cache-Control: no-cache` and `X-Accel-Buffering: no`, and no `id:` or `retry:` fields, so a stream
that breaks cannot be resumed: the client asks again.

Errors before a stream starts are ordinary responses with a JSON `{"detail": ...}` body and the
`X-Request-ID` header, whatever went wrong:

| Status | Meaning |
|---|---|
| 401 | missing or wrong bearer key (`WWW-Authenticate: Bearer`) |
| 404 | feedback: no such answer in this conversation. A missing id, a question's id and another conversation's answer all look the same |
| 409 | this conversation's previous answer is still being written |
| 422 | the request did not validate. The body says where, but never echoes what was sent |
| 429 | this conversation's rate limit; `Retry-After` in seconds |
| 503 | OpenAI or the database is unavailable, or the service is busy or restarting |
| 500 | a bug, or a configuration problem such as OpenAI rejecting the key |

### What the proxy has to do (build step 6)

The proxy is the portfolio site's Express API, `api/src/chat.js` in `mmihaylov94/my-portfolio`, with
tests in `api/tests/`. The browser sees two routes:

| Browser | Here | Body forwarded |
|---|---|---|
| `POST /api/chat` | `POST /v1/chat/stream`, always: the UI streams, and `/v1/chat` is not exposed | exactly `{session_id, message}` |
| `POST /api/chat/feedback` | `POST /v1/messages/{message_id}/feedback` | exactly `{session_id, rating, comment?}`; `message_id` travels in the browser's body and only the checked integer reaches the URL |

It validates both bodies the way this API does before anything is sent, so a request this API would
refuse never costs a round trip, and a 422 from here is logged there as drift between the two. These
are the things it must get right, and how it does:

- **Attach `Authorization: Bearer ${PORTFOLIO_AI_API_KEY}` server-side**, and never let the browser
  see it. The value must equal this service's `PORTFOLIO_AI_API_KEY`.
- **Send the visitor's real address in `X-Visitor-IP`.** Without a `trust proxy` setting, `req.ip`
  is Traefik's address, and a naively forwarded IP would hash identically for everyone. The proxy
  reads `TRUST_PROXY`: Cloudflare's proxy is on in front of mihaylov.io, so production is `2`
  (Cloudflare, then Traefik), which is right only once Traefik trusts Cloudflare's addresses
  (DEPLOYMENT.md §12). The value is parsed strictly, and every mistake falls back to trusting
  nothing, in which mode no `X-Visitor-IP` is sent at all. `User-Agent` and `Referer` go as
  `X-Visitor-User-Agent` and `X-Visitor-Referrer`, the referrer already cut to its origin and path.
- **Pipe the stream through unbuffered and uncompressed.** Read the upstream body as it arrives and
  write each chunk on, with `Content-Type: text/event-stream` kept. Do not add compression on this
  route; a compressor waits for enough bytes to be worth compressing, and SSE arrives as one chunk
  at the end. The proxy relays raw bytes with a reader loop, adds `Cache-Control: no-transform` so
  Cloudflare leaves the stream alone, and a test fails if the first event only arrives once the
  upstream has finished. It writes only whole events: bytes after the last blank line wait for
  the rest of their event, so a connection that breaks mid-event never hands the browser half of
  one. That rests on this API ending every event and ping with `\n\n`, line feeds only, which
  FastAPI's `fastapi.sse` does and `tests/unit/test_api.py` relies on; a server that switched to
  CRLF would have its whole answer held back. If the upstream breaks, goes quiet for 65 seconds,
  or runs past 200, the proxy ends the stream with an `error` event of the same shape as this
  API's own.
- **When the browser disconnects, stop reading the upstream.** Abort the fetch. The turn carries on
  here regardless and is recorded, so nothing is lost by letting go. In Express that is
  `res.once("close")`: `req.on("close")` fires as soon as the request body has been read.
- **Handle 409, 429 and 503** as "wait and retry" rather than as failures, and show the `detail`
  text. A 409 usually means the previous answer is still being written after the visitor reloaded
  the page.
- **Pass `X-Request-ID` into its own logs**, so a problem can be traced across both services. The
  proxy logs one line per request with that id, the statuses, an outcome and the duration, and
  nothing a visitor typed.
- **Put only an integer in the feedback URL.** Check that the browser's `message_id` is a positive
  integer before building `/v1/messages/{message_id}/feedback`. This API rejects anything else with a
  422 and logs only the route, but the proxy's own logs would still record whatever it put in the URL.
- **Keep a body size limit in front of this API**, which has none of its own. FastAPI reads a whole
  body before any check runs, the key included. The proxy limits its chat routes to 32 kB, room for
  2,000 characters even written entirely as escaped emoji.

## Which piece does what

| Module | Role |
|---|---|
| `api/main.py` | `create_app()`, which reads no settings, and the lifespan, which does: startup checks, the pool, and shutdown in order |
| `api/__main__.py` | `python -m portfolio_ai.api`, the production entry point: uvicorn with JSON logging, the event loop, graceful shutdown and keep-alive, all set in code |
| `api/routers/chat.py` | the two answer endpoints. They take nothing but the admission and translate events into JSON or SSE |
| `api/routers/feedback.py` | the rating endpoint |
| `api/routers/health.py` | `/healthz` and `/readyz` |
| `api/deps.py` | `admit_turn`: validation, the two reads, the checks in order, and the start |
| `api/turns.py` | `Turns`, the registry of running turns, and the task that drives one to the end |
| `api/security.py` | the bearer check, the IP hash, and reading the visitor headers |
| `api/errors.py` | the one mapping from exception to status and public sentence, used by the handlers and by the stream's `error` event |
| `api/schemas.py` | the Pydantic models for requests, responses and each event's data |
| `api/middleware.py` | the request id, and one log line per request |
| `db/chat.py` | `ensure_session` (now recording the visitor), `recent_questions`, `spend_since`, and the retention queries |
| `db/feedback.py` | `record_feedback`: the ownership check and the upsert, in one statement |
| `analytics/retention.py`, `analytics/cli.py` | `python -m portfolio_ai.analytics purge`, the nightly retention sweep |

`errors.py` exists separately from `main.py` only to avoid a circular import: `main.py` imports the
routers, and the streaming router needs the same mapping for its `error` event.

## The libraries, and how each is used

### FastAPI

**Dependencies** are FastAPI's central idea, and `Depends` is how everything here fits together. A
dependency is a function named in an endpoint's parameters. FastAPI calls it before the endpoint,
fills in *its* parameters the same way (request body, headers, other dependencies), and passes the
result in. `admit_turn` depends on the body and on `visitor` (the headers), and the endpoints
depend on `admit_turn`. The router adds `require_api_key` in front of all of them.

Their order matters in a way that is easy to trip over: **FastAPI validates an endpoint's own
parameters after running its dependencies.** If the chat endpoints took the body themselves, a
malformed body would be rejected with a 422 only after `admit_turn` had already started an answer,
which nobody would then read. So the endpoints take nothing but the admission.
`test_the_chat_routes_take_nothing_but_the_admission` fails if anyone changes that.

**Server-sent events** are built in since recent versions. An endpoint that is an async generator
and declares `response_class=EventSourceResponse` streams each yielded `ServerSentEvent` in the SSE
wire format, and inserts `: ping` keep-alive comments after 15 seconds of silence. Underneath,
FastAPI runs the generator in a producer task and cancels it cleanly when the client goes, which is
why cancellation here lands at an `await` inside the endpoint rather than somewhere surprising.

One rule follows from streaming: **the streaming endpoint must never raise.** Once the first event
is out, the status line (200) and headers are on the wire, and an exception would reach the client
as a response that stops mid-sentence. So the generator catches `Exception`, turns it into an
`error` event using the same mapping as everything else, and returns. `BaseException`
(cancellation) is not caught, because a visitor leaving should stop the reader.

**The lifespan** is an async context manager FastAPI runs around the whole server: setup before the
`yield`, teardown after it, in a `finally`. Settings are read there and not at import, which is why
tests can build a fresh app with no configuration at all. A missing API key or hashing key stops
startup with a message naming the variable.

**Exception handlers**, registered in `errors.install()`, turn `AssistantError`, `ConfigError` and
psycopg's `OperationalError` (which covers pool timeouts) into JSON responses. The 422 handler is
replaced as well: FastAPI's own echoes the rejected input back, which here would be a visitor's
message in an error body a proxy might log.

Anything else is a bug, and a handler registered for `Exception` answers it with the same JSON 500
as every other failure. Without that handler, Starlette's fallback answers in plain text, and a
proxy that reads every error body as JSON would fail on the one error it most needs to report.
Starlette treats an `Exception` handler differently from the rest. It runs in the outermost
middleware, outside `RequestLogMiddleware`, and raises the exception again once the response has
gone, so uvicorn still logs the traceback, with the request id. Being outside the middleware also
means the response would miss the `X-Request-ID` header, so the handler adds it itself.

The installed FastAPI (0.141) keeps an included router as a router rather than copying its routes
into `app.routes`, as older versions did. Tests that inspect routes read them from the router.

### Starlette and ASGI

FastAPI runs on Starlette, which implements ASGI: an app is any `async def app(scope, receive,
send)`. `scope` describes the request, `receive` is awaited for incoming messages (the body, or a
disconnect), and `send` is called with outgoing ones: first the status and headers, then the body in
pieces. `RequestLogMiddleware` is written at that level, wrapping `send` to see the status line and
add a header.

That is a choice, not a style preference. Starlette's friendlier `BaseHTTPMiddleware` (the
`@app.middleware("http")` decorator) runs the app in a task group of its own and wraps `receive`,
which changes what a disconnect does. An endpoint that would otherwise run to completion can then
be cancelled. The JSON endpoint relies on not being cancelled, so the middleware stays at the
protocol level and changes nothing about how the app runs.

### uvicorn

Production runs `python -m portfolio_ai.api`, not the `uvicorn` command, for three reasons:

- **Logging.** The `uvicorn` command always installs its own logging, plain text on stderr. From
  Python, `log_config=None` installs nothing, so its lines go through the JSON handler and every line
  the container writes is one JSON object, the first one included.
- **The event loop.** uvicorn picks the loop itself, before it imports the app: uvloop where it is
  installed (the production image), and on Windows the Proactor loop, which psycopg's async mode
  cannot use at all (`Psycopg cannot use the 'ProactorEventLoop'`). The fix in
  `portfolio_ai/__init__.py`, which changes asyncio's default, arrives too late for a loop uvicorn
  has already built. So the entry point names `asyncio:SelectorEventLoop`, which gives one loop on
  every machine: the one the tests and the command-line tools run on.
- **Settings in one place.** Graceful shutdown (30 s) and keep-alive (30 s, longer than Node's
  client-side idle timeout, so the server is never the one to close a connection the proxy is about
  to reuse) are set once, in code, rather than repeated as flags in the Dockerfile and both compose
  files.

The development server stays `uv run uvicorn portfolio_ai.api.main:app --reload`. With `--reload`,
uvicorn runs the app in a subprocess and picks the selector loop for it, so it works on Windows as
well.

Shutdown is budgeted against Docker's `stop_grace_period` of 45 s. On SIGTERM uvicorn stops
accepting connections and waits up to 30 s for open ones, such as streams mid-answer. Then the
lifespan marks the registry closing, waits up to 10 s for turns still running for visitors who left,
cancels whatever remains, and closes the OpenAI client and the pool, in that order.

### Pydantic

Request validation is all declared in `api/schemas.py` with `Annotated` types: the base type
first, then what runs on it, in order. For example:

```python
Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_CHARS),
    AfterValidator(_no_control_characters),
]
```

`AfterValidator` runs after Pydantic has checked the type; `BeforeValidator` runs on the raw
input, before it. The rating uses the second: it has to see `true` before the `Literal[-1, 1]` check
quietly reads it as `1`. A validator raises `ValueError` to reject a value. Pydantic turns that into
a 422, but lets a `TypeError` escape as a crash, which is why `_not_a_boolean` raises the "wrong"
exception on purpose.

The settings gained `hide_input_in_errors=True`. By default a validation error quotes the offending
input. For settings that input is often a secret, and the local port check used to print the whole
input dictionary, database password and OpenAI key included, into the traceback the API logs when
it fails to start.

### asyncio

- `asyncio.create_task` starts a turn without awaiting it, and the registry keeps the reference.
- `asyncio.Queue` connects a turn to its reader. It is unbounded, because the producer must never
  wait on a reader that may have gone, and `put_nowait` never blocks.
- `asyncio.timeout(120)` is each turn's deadline. The SDK allows 30 s a call with three retries, and
  an answer is up to five calls, so without a deadline one stuck turn could hold its conversation's
  slot for several minutes.
- `asyncio.wait(tasks, timeout=...)`, then `cancel()` and `gather(..., return_exceptions=True)`, is
  how shutdown drains: wait a while, cancel the rest, then wait for the cancellations to land.

### The standard library

- `secrets.compare_digest` compares keys in constant time, on bytes. On a `str` it raises for
  non-ASCII input, and header values are whatever the caller sent.
- `hmac` + `hashlib.sha256` hash IP addresses with a secret key. That is HMAC's job, and a plain
  hash of an IPv4 address is reversed by hashing all four billion of them.
- `ipaddress` parses an address before hashing, so every spelling of the same address (IPv6 case,
  zero compression, IPv4-mapped) hashes the same.
- `urllib.parse.urlsplit` cuts a referrer down to its page.
- `http.HTTPStatus` names status codes (`HTTPStatus.SERVICE_UNAVAILABLE`) instead of bare numbers.

## The guarantees, and where each one lives

| Guarantee | Where |
|---|---|
| No request is served without the key; no key configured means nothing is served | `security.require_api_key`, `security.require_api_secrets` |
| Every started turn is stored and counted, even if the visitor leaves | `turns.py` |
| One turn at a time per conversation, with no lock | `deps.admit_turn`, no `await` between check and start |
| A 422 cannot abandon a started turn | the endpoints take nothing but the admission |
| The daily spend limit bounds the bill; it can be overshot only by what is already in flight, at most `MAX_CONCURRENT_TURNS` answers | `deps.admit_turn`, `db.chat.spend_since` |
| A stream always ends with `done` or `error` | `routers/chat.py`, and `turns._run`, which tells the reader before it logs |
| Nothing internal reaches a response body, and every error body is JSON | `errors.py` |
| No raw IP is ever stored | `security.hash_ip`; `db/chat.py` is never handed one |
| No visitor text in the logs | the middleware logs method, route template and status only; tested by a canary |
| Chat older than `CHAT_RETENTION_DAYS` is deleted nightly | `analytics/retention.py`, `docker/crontab` |

Failed turns are the one gap. A turn that fails part-way stores nothing, so what it had already
spent counts against neither limit. Failures are rare and usually mean nothing was spent, and the
Express per-IP limit bounds the rest. A monthly budget on the OpenAI project is the backstop behind
all of it.

## Retention

`python -m portfolio_ai.analytics purge` runs nightly at 03:00 (`docker/crontab`). It deletes chat
messages created before `CHAT_RETENTION_DAYS` ago (90), and sessions last seen before then. Those
carry the hashed address and the browser string. The foreign keys decide the rest, each chosen
with this sweep in mind:

- **Feedback** cascades with the answer it rated.
- **An answer** cascades with its question (`reply_to_id`), even if the answer was written a few
  seconds on the recent side of the cutoff. Half a turn is not worth keeping.
- **A question promoted into the eval dataset** survives: `eval_cases.source_message_id` is `on
  delete set null`, so the test case outlives the conversation it came from.

`--dry-run` counts with the same cutoff and deletes nothing. `CHAT_RETENTION_DAYS=0` turns the sweep
off, and the job then logs a warning every night, since the privacy policy is quietly not being
kept.

The CLI has a Typer callback that does nothing, and the crontab depends on it. A Typer app with a
single command and no callback runs that command directly, so `purge` would be rejected as an
unexpected argument.

## How it is tested

- **`tests/unit/test_api.py`** drives the real app through `httpx.ASGITransport`, with the assistant
  replaced by a script and the two admission reads by values the test sets:
  - auth, every kind of 422, each admission check;
  - the two-requests-at-once race;
  - JSON and SSE answers, failures before and after the first token, and a bug as a JSON 500;
  - `/readyz` with the database up and down;
  - the `X-Request-ID` header, and a request log line that names the route rather than the path;
  - keep-alive pings, feedback;
  - the lifespan refusing to start without its secrets, and shutting down in order.
- **A disconnect test drives the ASGI app by hand.** It sends a scope shaped as uvicorn sends it,
  delivers the body, then a disconnect as soon as the first event arrives. The request returns, the
  turn is still running, and after it is released, it finishes. httpx cannot hang up halfway, so
  this is the only way to test the guarantee through the real endpoint. A second test hangs up the
  same way and checks that the turn still carries the request id after the request has gone.
- **`tests/unit/test_turns.py`** tests the registry directly: abandoned readers, deadlines,
  shutdown, a failure that still reaches the reader when logging it raises, and a late callback
  that must not evict a newer turn.
- **`tests/integration/test_api_db.py`** checks the SQL against Postgres: the rate window and
  `Retry-After` arithmetic, the rolling spend, the feedback ownership check, and the retention sweep
  with its cascades.
- **`tests/integration/test_api_endpoints.py`** runs everything real except OpenAI:
  - stored ids match the ones returned;
  - the limits count the rows the API wrote;
  - a visitor who left is still stored;
  - a privacy canary: a distinctive message, comment and IP must not appear anywhere in the JSON
    logs the process writes.
- **CI** builds the image and starts it as production does, then asks it for `/healthz`. The
  database it is pointed at does not exist, and that is the point: the server must start regardless
  and say so on `/readyz`, rather than restart in a loop during an outage.

## Running it

Development, from the repository root:

```bash
uv run uvicorn portfolio_ai.api.main:app --reload --reload-include "*.md"
```

`--reload-include "*.md"` also restarts on prompt edits, since uvicorn watches only `*.py` by
default. `/docs` is the browsable schema, with an "Authorize" button for the key.

In PowerShell, with the key read from `.env` and the body in a file. That sidesteps Windows
PowerShell's habit of stripping the double quotes out of JSON passed to a native program. The file
goes in the temp folder so it cannot end up in a commit.

```powershell
$key = (Select-String -Path .env -Pattern '^PORTFOLIO_AI_API_KEY=(.+)$').Matches[0].Groups[1].Value.Trim()
'{"session_id":"local-check-0001","message":"Tell me about Threadline"}' | Set-Content -Encoding ascii "$env:TEMP\ask.json"

curl.exe -N http://127.0.0.1:8000/v1/chat/stream -H "Authorization: Bearer $key" -H "Content-Type: application/json" --data-binary "@$env:TEMP\ask.json"
curl.exe http://127.0.0.1:8000/v1/chat -H "Authorization: Bearer $key" -H "Content-Type: application/json" --data-binary "@$env:TEMP\ask.json"
```

As production runs it (JSON logs from the first line):

```bash
uv run python -m portfolio_ai.api
```

The retention sweep:

```bash
uv run python -m portfolio_ai.analytics purge --dry-run
```
