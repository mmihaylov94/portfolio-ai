# The assistant, end to end

How a question becomes an answer: what happens during one turn, which module does which part, and
what each library is used for. Today the assistant runs from a terminal
(`python -m portfolio_ai.assistant`) and behind the HTTP API ([API.md](API.md)), and the API
wraps exactly what is described here. The reasoning behind the design is in
[ARCHITECTURE.md](../ARCHITECTURE.md) §8. This document is about the code.

## The idea it rests on

**An answer is a stream of events, passed up through a stack of async generators.** Each layer
reads the events of the layer below it with `async for`, adds one thing, and `yield`s its own
events upward. No layer waits for the answer to be finished before passing it on.

```
cli.py, api/turns.py                prints each event / sends it to the browser
 └─ conversation.chat()             + history before, storage after, the stored message id
     └─ agent.respond()             + classification, the link filter, the final result
         └─ agent._route()          + one of three paths, including the search loop
             └─ responses.stream()  OpenAI's event stream, reduced to "some text" and "done"
```

An async generator is a function defined with `async def` that uses `yield`. Calling one runs
none of it. It hands back an object you iterate with `async for`, and each step of the iteration
runs the function until its next `yield`, waiting on the network in between without holding up
anything else. If you know `async function*` and `for await` from JavaScript, it is the same idea.
PHP has generators, but nothing that can wait on I/O inside one.

The design was chosen because three requirements all want this shape:

- A visitor should see the first words while the rest is still being written, so every piece of
  text is yielded upward the moment OpenAI sends it.
- The API should have nothing to assemble. It forwards the events to the browser as server-sent
  events, one for one (see [API.md](API.md)).
- The evals must not write chat rows. They call `respond()`, one layer below where anything is
  stored, and keep the search events and the final one (see [EVALS.md](EVALS.md)).

The events themselves are four small classes in `agent.py`:

| Event | When | Carries |
|---|---|---|
| `Classified` | once, first | the route: `out_of_scope`, `small_talk` or `mihail_related` |
| `Searched` | once per search | the query the model wrote, the chunks found, the best score |
| `Token` | many times | a piece of the answer, already through the link filter |
| `Done` | once, last | a `TurnResult`: the whole answer, its evidence and its cost |

`agent.py` names them together as `type Event = Classified | Searched | Token | Done`. That is
Python 3.12's syntax for a type alias, and `|` makes a union, as in TypeScript.

## One question, start to finish

Follow "What projects has Mihail worked on?" through a saved conversation. The timings are from a
real run on 2026-09-22, with the default settings.

**Memory.** `conversation.chat(session_id, message)` finds or creates the session's row
(`db.chat.ensure_session`, which the API also hands where the visitor came from, recorded once)
and loads its last 50 messages (`load_recent_messages`): 25 exchanges,
which is what n8n's memory setting meant. History holds questions and final answers only, as n8n
stored it. The searches behind earlier answers are not replayed, because a follow-up gets a fresh
search written for the question actually being asked. The database connection goes back to the
pool before any model is called: the calls take seconds, and the pool is shared.

**Classify.** `agent.respond()` starts by calling `classifier.classify()` with the new message and
the previous exchange — the last question and its answer, if there are any. n8n gave its
classifier the message alone, and so sent "yes please", the natural reply to Rachel's own offer of
more detail, to small talk, the one route that cannot search. The call goes through
`llm.responses.parse()` with the classifier prompt and `text_format=MessageCategory`. The prompt
is n8n's plus two additions in version 2, made after the evals: it names Mihail's projects, since
the classifier sees one message and would otherwise refuse "What is Glotsmith?" as off-topic, and
it says a short reply such as "yes please" is classified by what it accepts.
`MessageCategory` is a Pydantic model with a single field that can only hold one of the three
labels. The SDK turns it into a JSON schema, the API makes the model answer in exactly that shape,
and the SDK parses the reply back into a `MessageCategory`, so the label cannot arrive misspelled.
If the model refuses or the reply cannot be read, the classifier picks `mihail_related`, following
the prompt's own rule for when in doubt. This took 5.6 seconds, and `respond()` yields
`Classified("mihail_related")`.

**Route.** `_route()` sends the message down one of three paths. `out_of_scope` yields the fixed
reply from `out_of_scope_reply.md` and makes no further calls. `small_talk` streams one reply from
the small-talk prompt, with the conversation for context and no tools. `mihail_related` runs the
search loop, which is where this question goes.

**The forced search.** `_knowledge_base_answer()` calls `responses.stream()` with Rachel's prompt,
the conversation, the question, the `search_knowledgebase` tool, and `tool_choice="required"`,
which obliges the model to call a tool. It replies with a function call,
`search_knowledgebase({"query": "projects portfolio case studies work history"})` — the query
rewriting that `search_tool.md` asks for, with the filler and the name stripped out. Alongside it
comes a reasoning item: the model's working, encrypted, because every call is made with
`store=False` (more on that below). Any text the model writes during this call is discarded, so
nothing reaches the visitor from it. 2.8 seconds.

**Search.** `_run_search()` passes the query to `retrieval.search_knowledgebase()`, which embeds it
with the same `embed_texts()` ingestion uses and then runs the one query in the assistant that
reads the knowledge base, `db.documents.search_chunks()`:

```sql
select c.id, c.section, c.section_title, c.content,
       d.doc_id, d.title, d.url,
       1 - (c.embedding <=> %(query)s) as score
from chunks c
join documents d on d.id = c.document_id
order by c.embedding <=> %(query)s
limit %(top_k)s
```

`<=>` is pgvector's cosine distance, the operator the HNSW index from migration 0001 was built
for; ordering by any other operator would bypass the index and score every row. `1 - distance`
turns it into a similarity, where bigger means closer. The top 20 chunks go back to the model as a
`function_call_output` tagged with the call's id, holding each chunk's title, section, URL and
content as JSON, without scores, which is how n8n presented them. `respond()` yields `Searched`.
1.5 seconds.

**Answer.** The loop calls `responses.stream()` again with everything appended to the input: the
question, the model's own reasoning and function call from the previous call, and the results.
Sending the reasoning back is the reason this uses OpenAI's Responses API: the answering call
carries on from why the model searched for what it did, rather than starting cold. `tool_choice`
is now `"auto"`, so the model may search again, up to `AGENT_MAX_SEARCH_ROUNDS` searches in all
(3); after the last one it is made to answer. Here it answers straight away, and the text streams
back as it is written. 16.9 seconds, most of them spent reasoning before the first word.

**Filter.** Each piece of text passes through a `LinkFilter` before `respond()` yields it as a
`Token`. The filter takes out any URL containing `/knowledgebase/` or `/projects/`, and it can do
that mid-stream because it holds back only what might still turn out to be part of such a URL: the
word in progress, or a Markdown link whose `[` has opened and not yet closed. Everything before
that goes out at once.

What goes in the link's place is the reason the filter replaces rather than deletes. By the time
a URL arrives, the words that led up to it ("you can read about it at") have already been sent, so
deleting the URL left "read about it at ." behind, and "at **." when the link was in bold. A bare
forbidden URL now becomes the nearest real page, the site's projects section for a `/projects/`
URL and its home page otherwise, with the emphasis, brackets and punctuation around it left where
they were. Both pages are on the prompt's own list of allowed links, and a unit test keeps them
there. A Markdown link to a forbidden page keeps its label and loses the link instead: "[the case
study](…)" pointing at the projects section would promise a page it isn't.

**Result.** When the stream ends, `respond()` builds a `TurnResult` — the reply as the visitor saw
it, the route, each search, the chunk ids, the best score, whether the reply was the "I don't have
that information" fallback, up to three citations, the usage of every call, and the timings — and
yields `Done(result)`.

**Storage.** `chat()` catches that `Done`, writes the question and the answer as two rows in one
transaction (`db.chat.insert_turn`), and yields a copy of the `Done` whose result carries the new
row's `message_id`, which is what the browser will need to attach a thumbs-up to the answer.
`TurnResult` is frozen, so the copy is made with `dataclasses.replace()`, which builds a new
instance with one field changed.

The whole turn took 26.7 seconds, with the first word at 22.4, and cost $0.0040. This is the
per-call record stored for it in `chat_messages.llm_calls`:

| Step | Call | Time | Tokens in | Tokens out (reasoning) |
|---|---|---|---|---|
| `classify` | structured output, `classifier@1` | 5.6s | 582 | 69 (0) |
| `search` | streamed, tool required, `rag_agent@1` | 2.8s | 1,531 | 100 (64) |
| `embed` | `text-embedding-3-small` | 1.5s | 6 | — |
| `answer` | streamed, `rag_agent@1` | 16.9s | 5,299 | 886 (704) |

The answering call reasons through 704 tokens before it writes a word, and that is where most of
the wait is. See [What the numbers look like](#what-the-numbers-look-like).

## Which piece does what

| Module | What's in it | Its job |
|---|---|---|
| `assistant/prompts/loader.py` and `*.md` | `Prompt`, `load()`, `parse()`, `CLASSIFIER`, `SMALL_TALK`, `RAG_AGENT`, `SEARCH_TOOL`, `OUT_OF_SCOPE_REPLY`, `ALL` | The five prompt files, read once at import, each with a version |
| `llm/responses.py` | `parse()`, `stream()`, `CallUsage`, `Parsed`, `FunctionCall`, `TextDelta`, `Completed`, `domain_error()` | Every chat-model call: usage recorded, stream simplified, errors translated |
| `llm/pricing.py` | `ModelPrice`, `PRICES`, `cost_usd()` | Dollars from tokens, including cached input |
| `assistant/classifier.py` | `MessageCategory`, `ClassifierResult`, `classify()` | Route a message, given the exchange before it |
| `assistant/memory.py` | `HistoryMessage`, `window()`, `previous_exchange()`, `as_input()` | Shape the conversation for the model |
| `assistant/retrieval.py` | `TOOL`, `SearchResult`, `search_knowledgebase()` | The search tool: embed, search, skip repeats, format for the model |
| `db/documents.py` | `RetrievedChunk`, `search_chunks()` | The similarity query |
| `assistant/postprocess.py` | `LinkFilter`, `strip_forbidden_links()`, `is_fallback()` | The link rule, and spotting the fallback answer |
| `assistant/agent.py` | `AssistantConfig`, `Classified`, `Searched`, `Token`, `Done`, `TurnResult`, `Citation`, `Search`, `respond()` | One answer, start to finish, writing nothing |
| `assistant/conversation.py` | `chat()` | Memory in, answer out, the turn stored |
| `db/chat.py` | `ensure_session()`, `load_recent_messages()`, `insert_turn()` | Every SQL statement for the chat tables |
| `assistant/cli.py`, `__main__.py` | `main()`, `_Chat` | The terminal chat |

Three of them deserve a little more than a table cell.

`AssistantConfig` gathers the knobs — both models, both reasoning efforts, `top_k` and the number of
search rounds — into one frozen object, built from the settings by default. It exists for the evals:
a run can ask the same questions with `chat_model="gpt-5"` without touching the environment, and
stores `config.as_record()`, which includes every prompt's version, alongside the scores.

`llm/responses.py` is the only module that talks to OpenAI's chat models, and it is deliberately
thin. It doesn't retry, because the SDK already does. It exists so that each call site doesn't have
to repeat three things: recording a `CallUsage` (tokens split into cached and reasoning, latency,
cost, and which prompt version was used), reducing a dozen kinds of stream event to `TextDelta` and
`Completed`, and turning any failure into one domain error.

The dictionaries sent to OpenAI — `{"role": "user", "content": "..."}`, the tool definition, the
function-call outputs — are typed with the SDK's `TypedDict`s, such as `EasyInputMessageParam` and
`FunctionToolParam`. At runtime they are ordinary `dict`s. The class only tells mypy which keys and
value types are allowed, much like a TypeScript interface describing an object literal.

## The libraries, and how each is used

**The OpenAI SDK's Responses API** carries every chat call, in `llm/responses.py`. The classifier
uses `client.responses.parse(...)`, which takes a Pydantic class as `text_format` and hands back an
instance of it as `response.output_parsed`. Everything that streams uses
`client.responses.create(..., stream=True)`, which returns an async iterator of typed events.
`stream()` reads four kinds — a text delta, and the response completing, failing or stopping
early — and turns them into `TextDelta` and a final `Completed`. The Rachel prompt goes in
`instructions`, the conversation in `input`.

n8n used the older Chat Completions API. The Responses API was chosen because the model's
reasoning can be handed from the call that searched to the call that answers. Every call is made
with `store=False`, which means OpenAI does not keep the response. The cost of that is that
nothing can be referred to by id later, so the reasoning is requested in encrypted form
(`include=["reasoning.encrypted_content"]`) and sent back inside the next request.

The SDK also does the embedding, through `embed_texts()` from ingestion. It retries connection
errors, timeouts, rate limits and server errors on its own, three times, as configured in
`llm/client.py`. Whatever still fails arrives as an `openai.APIError`, which `domain_error()` turns
into an `AssistantError`, or a `ConfigError` if the key was rejected. The API then has one error
to translate into a polite "try again in a moment" (`api/errors.py`). One surprise in the installed
version, 3.16: the SDK does its HTTP through `httpx2`, a separate package from the `httpx`
ingestion uses. It wraps network failures in its own exceptions, so catching `APIError` is enough,
but it is also why the test fake is built on `httpx2`.

**Pydantic** defines `MessageCategory`, and the SDK derives the JSON schema the model must answer
in from it. The class has no docstring on purpose: a model's docstring becomes the schema's
description, and the classifier would read it on every message.

**psycopg 3 and pgvector** run both queries. `search_chunks()` sends the query vector wrapped in
`pgvector.Vector(...)` rather than as a plain list. psycopg sends a list as a `float8[]` array, and
pgvector only converts arrays to vectors when writing into a column, as ingestion does. In a
comparison there is no `vector <=> float8[]` operator, and the query fails. The named placeholder
`%(query)s` appears twice in the query and is sent once. `insert_turn()` writes the `jsonb`
columns through `psycopg.types.json.Jsonb(...)`, a Python list into `bigint[]`, and a `Decimal`
into `numeric`, all inside `async with pool.connection() as conn, conn.transaction():`.

**PyYAML** reads the small header at the top of each prompt file, with `yaml.safe_load`.

**typer** builds the chat command's flags from its function signature, as it does for ingestion.

**structlog** writes one `llm_call` line per model call and one `turn_answered` line per answer.
They contain numbers only — route, score, tokens, cost, timings — and never the question or the
answer, because the database has a retention policy and logs do not.

From the standard library: `asyncio.Runner` keeps one event loop alive across a whole terminal
conversation (see `cli.py`); `dataclasses`, including `replace()`; `importlib.resources` finds the
prompt files inside the installed package; `hashlib` fingerprints each prompt for the test that
pins its version; `re` drives the link filter; `json` reads the tool's arguments and writes its
output; `decimal` for money; `time.perf_counter` for every timing; `uuid` for new session ids.

`cli.py` is also where Python's `match` statement appears, and it is worth a second look, because
it does more than PHP's `match`:

```python
match event:
    case Token(text):
        typer.echo(text, nl=False)
    case Searched() if self.verbose:
        _show_search(event)
    case Done(result):
        ...
```

`case Token(text)` checks that `event` is a `Token` and pulls its field into `text` in the same
step. It works on these classes because `@dataclass` generates the `__match_args__` attribute that
says which fields the positions refer to. PHP's `match` only compares values, and JavaScript has
no equivalent.

## What every answer records

Two rows per turn in `chat_messages`, written together. Most of these columns exist for the
analytics in step 7, which can only use what was recorded when the answer was given.

| Column | Row | What it holds |
|---|---|---|
| `classification` | both | the route |
| `reply_to_id` | answer | the question's row, so feedback on an answer can find what was asked |
| `top_score` | answer | the best similarity of any search; empty if nothing was searched |
| `fallback_used` | answer | whether the reply was "I don't have that information" |
| `retrieved_chunk_ids` | answer | every chunk shown to the model |
| `tool_calls` | answer | each search: query, hits, best score, duration |
| `model` | answer | the model that wrote the answer, as the API names it (`gpt-5-mini-2025-08-07`) |
| `prompt_tokens`, `completion_tokens` | answer | summed over the chat calls |
| `cost_usd` | answer | everything, embeddings included |
| `latency_ms`, `first_token_ms` | answer | the whole answer, and how long until the first word |
| `llm_calls` | answer | one record per OpenAI call: step, model, prompt version, tokens, latency, cost |

## The guarantees, and where each one lives

**The first search is forced.** `_knowledge_base_answer()` sends `tool_choice="required"` on its
first call. The prompt already demands a search before any factual answer; this makes it certain
rather than likely, and it is why every `mihail_related` answer has a `top_score`.

**Forbidden links never reach a visitor.** The prompt forbids them twice, and the `LinkFilter` in
`respond()` removes any that get through, as the text streams. Its tests check that the streamed
output equals the output of cleaning the whole text at once, for a set of awkward samples split at
every position, fed one character at a time, and cut up at random a few hundred times each.

**Nothing is kept on OpenAI's side.** Every call in `llm/responses.py` sends `store=False`.

**No database connection is held while a model works.** `chat()` reads before the first call and
writes after the last one.

**Logs never contain what anyone said**, only counts, scores, costs and timings.

**Test questions cannot pollute production analytics.** The terminal chat refuses to run with
`ENVIRONMENT=production` unless given `--no-save`.

## What the numbers look like

Reasoning effort matters more than anything else here. The same question, with both efforts set
to the same value:

| Reasoning effort | First word | Total | Cost |
|---|---|---|---|
| default (medium, which is what n8n sends) | 22.4s | 26.7s | $0.0040 |
| `low` | 7.8s | 10.6s | $0.0028 |
| `minimal` | 5.9s | 9.2s | $0.0024 |

All three made the same search and named the same projects. The evals have since measured what
the lower settings cost in quality: at `minimal`, almost nothing (EVALS.md, Results). Production's
`.env` now sets `minimal`; the code default stays at n8n parity. `CHAT_REASONING_EFFORT` and
`CLASSIFIER_REASONING_EFFORT` in `.env` change it for a session.

Two things about retrieval are worth knowing before reading any scores. First, the scale depends on
how the query is phrased. On this corpus, the model's keyword-style queries score around 0.4 to 0.5
for their best match, and the same questions searched as asked score 0.6 to 0.7. So `top_score`
is only comparable between queries of the same style, and any threshold for flagging content gaps
has to be set against the queries the system actually sends.

Second, the query rewriting inherited from n8n is doing real work, and it also has a failure mode.
For three of the rewriting examples in the n8n prompt itself, this is where the sections that
answer each question ranked:

| Question | Searched as asked | Searched as rewritten |
|---|---|---|
| "Tell me about Mihail's management experience" | 2nd | 1st |
| "What projects has Mihail worked on?" | 44th | 1st |
| "Does Mihail work with Laravel?" | 2nd and 4th | 10th and 16th |

Asked as written, the projects question scores almost equally against every heading shaped like
"What … Mihail …?", whatever its topic: services, contact details and experience level all land at
0.65 to 0.66. The section that answers it, "What projects are on the portfolio?", doesn't mention
him, and comes 44th. Rewriting strips the name and the question form, and fixes that. For
Laravel, though, the keywords match individual projects' tech-stack sections better than the
general one. With
`top_k = 20` both Laravel sections are still retrieved and the answer is right; at `top_k = 5`
they would not be. These are three examples, not an evaluation; an eval run at a lower `--top-k`
measures it properly. Until one has: be careful about reducing `top_k`.

## How it is tested

No test calls OpenAI. `tests/openai_fake.py` provides `FakeOpenAI`, which stands in for
`api.openai.com` through an `httpx2.MockTransport` placed underneath the real SDK. A test scripts
the replies in order —

```python
fake.classify("mihail_related")
fake.search("Laravel PHP experience")
fake.say("Yes, Laravel is his main PHP framework.")
```

— and afterwards reads `fake.requests` to check what was actually sent: which instructions, what
input, which `tool_choice`. Because only the network is replaced, the real SDK builds every request
and parses every response, including the server-sent event stream.

The unit tests cover the adapter, the classifier's input, every route, the search loop and its
round limit, the link filter (including the every-split test above), fallback detection, prompt
loading and version pinning, pricing, and the terminal command. The integration tests run the
similarity query against a real pgvector index, the chat tables' storage, and a two-turn
conversation end to end with only the model faked. That last one checks that the second
classification is sent the first exchange.

## Running it

```bash
uv run python -m portfolio_ai.assistant                 # chat, saved to the configured database
uv run python -m portfolio_ai.assistant -v              # also show what each search found
uv run python -m portfolio_ai.assistant --no-save       # read and write nothing in the chat tables
uv run python -m portfolio_ai.assistant -m "..."        # ask one question and exit
uv run python -m portfolio_ai.assistant --session ID    # continue an earlier conversation
```

In the chat, `/new` starts a new session and `/exit` leaves. A real answer with `-v`, at the
default settings:

```
rachel>
  searched "Laravel experience PHP"
    0.406  Threadline / Technology stack
    0.378  Threadline / What technologies does Threadline demonstrate?
    0.321  AI Marketing Reporter / Technologies
    0.318  About Mihail Mihaylov / What does Mihail actually do?
    0.311  n8n Pro Automation Framework / Technologies
  rachel> Yes — Mihail works with Laravel as part of his PHP backend experience. His public tech stack lists "PHP (CodeIgniter, Laravel)" and his portfolio includes PHP/CodeIgniter work (Threadline) as an example of his backend projects. You can view his site for details: https://mihaylov.io/ or the Threadline demo at https://threadline.mihaylov.io — I can pull up specific Laravel examples if you’d like.
  mihail_related · searched "Laravel experience PHP" top 0.41 · 7,161 in / 846 out · 4 calls · $0.0035 · first word 17.6s · 20.4s
```

`-v` shows the top five of the twenty chunks sent to the model, and none of those five mentions
Laravel. The two sections that do were 9th and 13th, which is the `top_k` point above in practice.

The line under each answer reads left to right as: the route; each search's query and best score;
tokens in and out across the chat calls (with cached input shown when there is any); how many
OpenAI calls the answer took; what it cost; when the first word arrived; and the total time. It can
also end with `fallback`, a count of links the filter removed, and, when the turn was saved, the
answer's `message` id.
