# CLAUDE.md

Context for Claude Code working in this repository. Read [ARCHITECTURE.md](ARCHITECTURE.md) for
the full design; this file is the short version plus the working agreements.
[docs/INGESTION.md](docs/INGESTION.md), [docs/ASSISTANT.md](docs/ASSISTANT.md),
[docs/API.md](docs/API.md), [docs/EVALS.md](docs/EVALS.md),
[docs/ANALYTICS.md](docs/ANALYTICS.md) and [docs/CONTACT.md](docs/CONTACT.md) walk through the
code of the six flows, module by module -- update them in the same change as the code they
describe. API.md also holds the contract the Express proxy has to meet, for the chat and for
the contact form.

## What this project is

A production Python system that replaces two n8n workflows powering the AI chat box on
[mihaylov.io](https://mihaylov.io), plus an eval harness and an analytics loop, and since
October 2026 the site's contact form. Five deliverables:

1. **Ingestion** — pull Markdown from the `knowledgebase/` folder of the GitHub repo
   `mmihaylov94/my-portfolio`, chunk it by H2 section, embed it with OpenAI, upsert into
   Postgres/pgvector. Runs hourly in production.
2. **Assistant** — a RAG chat API ("Rachel") answering questions about that documentation.
   Authenticated endpoint called by the portfolio site's chat box.
3. **Evals** — an ad-hoc CLI that scores the assistant against a golden dataset so different
   models and configs can be compared on retrieval quality, answer quality, latency and cost.
4. **Analytics & feedback** — capture what visitors ask, thumbs up/down on answers, and derived
   failure signals (low retrieval score, fallback answers, rephrase-and-retry), cluster them
   into content gaps, and feed real failures back into the eval dataset.
5. **Contact form** — each message from the site's form: fixed spam rules ported from n8n, a
   classifier, a short automatic acknowledgement to the visitor, and an email to the owner for
   every message, blocked ones included. Nothing is stored. Added on 2026-10-13, when it was
   the last thing the site needed n8n for.

The exported n8n workflows are the functional spec for deliverables 1, 2 and 5. They sit in
`n8n_workflows/` locally but are **gitignored** — they carry n8n instance and credential ids, and
the repo is public. They are on disk, so read them before changing ingestion or prompt behaviour;
the prompts there are well tuned and port over mostly verbatim. If the folder is ever missing,
ARCHITECTURE.md §2 transcribes everything that matters. Deliverable 4 is new; nothing like it
exists today despite what the portfolio's README claims.

## Two repos, both in scope

This project spans two codebases:

- **`C:\Users\mmiha\Programming\ai_assistant`** (this one) — the Python backend.
- **`C:\xampp\htdocs\my-portfolio`** — the Nuxt 4 site and its small Express API. **Also being
  changed as part of this work**, so its current state is a starting point, not a constraint.
  It has its own `CLAUDE.md` worth reading before touching it.

Current state there, as of 2026-10-09: **the cutover happened on 2026-10-09** (docs/DEPLOYMENT.md
§12, steps 1 to 5). The live chat is the site's own chat UI, which calls `/api/chat` and
`/api/chat/feedback` on the Express API, and those proxy to this service. The site is fully
prerendered with no Nitro runtime in production; the conversation lives in
`localStorage["portfolio-chat/v1"]`. The owner switched n8n's two chat workflows off and dropped
`mihaylov_chat_histories` on 2026-10-13 (step 7 there), so the old widget is no longer a
rollback. **The old widget had no feedback**, despite what the old README claimed.

The contact form switched over on 2026-10-14 (build step 8). `api/src/contact.js` there hands
each message to this service's `/v1/contact`, in place of the n8n webhook it used to call, and
n8n's contact workflow is deactivated and kept as the rollback (docs/DEPLOYMENT.md §12, "The
contact form"). **Nothing on the site depends on n8n any more.**

Settled cross-repo decisions:

- **The proxy is the existing Express API.** It gained `/api/chat` and `/api/chat/feedback` in
  step 6 (`api/src/chat.js`, beside its contact handler): attach the bearer key server-side,
  forward to FastAPI. Nuxt rendering, the nginx image and Traefik routing are all untouched.
  **FastAPI gets no routing labels**, only `traefik.enable=false` — it is private to the Docker
  network. A Nitro route, a public FastAPI and minted session tokens were considered and rejected
  (ARCHITECTURE.md §3).
- **The chat UI is a clean-sheet redesign.** `@n8n/chat` is removed entirely, along with the
  DOM-querying in `useAiChat.ts` and the MutationObserver that injects the "Start over" button.
  Nothing about the old widget's UX needs reproducing.
- **Existing conversations are not migrated.** `session_id` is just `crypto.randomUUID()` in
  `localStorage`; `mihaylov_chat_histories` went when n8n's chat workflows were retired.
- **The contact form's flow lives here, and Express stays its door.** `/api/contact` keeps its
  per-address limit and reCAPTCHA, attaches the key and forwards four fields. The rules, the
  two model calls and both emails are this service's (docs/CONTACT.md). Two things are stated
  on both sides and change together: the fields' lengths (`api/schemas.py` here; `MAX_CHARS`
  in `api/src/contact.js` and the form's `maxlength` there), and the wait (35 seconds there;
  `CLASSIFY_SECONDS`, `REPLY_SECONDS` and `SEND_SECONDS` twice here, 30 in all). A test on
  each side holds its number.
- **Article validation lives here, runs there.** `portfolio-ai-validate` is a command in *this*
  package that calls the same `parse_document` and `chunk_document` the pipeline uses, so
  "passes CI" and "will be indexed" cannot drift apart. The portfolio repo runs it from the
  published image in `.github/workflows/knowledgebase.yml` on any commit touching
  `knowledgebase/**`. **Never reimplement the rules over there** — a checker that disagrees with
  the real parser is worse than no checker.
- **`.claude/skills/knowledgebase-articles/` in the portfolio repo** carries the authoring
  conventions the validator cannot check: H2 headings as questions, self-contained sections,
  `doc_id` permanence, and why each section becomes one chunk.

Watch out when proxying the streaming endpoint through Express: the response must be piped, not
buffered, and compression disabled on that route, or SSE arrives as one chunk at the end.

## Owner context — important

The owner is an experienced developer but **new to Python**, and one of the explicit goals is to
learn Python and Python-for-AI properly. That changes how to work here:

- **Explain the Python-specific parts.** Idioms, stdlib choices, async patterns, typing, packaging,
  why a decorator or context manager is the right tool. Do not explain general programming.
- **Prefer transparent code over framework magic.** This is why there is no LangChain: every
  embedding call, SQL query and agent loop iteration should be visible and readable.
- **Do not over-abstract.** No base classes or plugin registries until there is a second
  implementation that needs them.
- When there is a genuine Python choice to make (sync vs async, dataclass vs Pydantic model,
  `TypedDict` vs class), say which and why in one or two sentences, then get on with it.
- **Review before the owner does.** When a change is ready to hand over, run the `code-reviewer`
  agent ([.claude/agents/code-reviewer.md](.claude/agents/code-reviewer.md)) on it first, fix or
  answer every finding, and include its report in the handover. It starts without the session
  that wrote the code, which is the point: it checks the repo's own rules, the docs against the
  code and the tests' isolation, and proves what it can by running. **Never use Claude Code's
  built-in `/code-review`** in this repository, in any form; the owner has ruled it out.

## Stack

| Concern | Choice |
|---|---|
| Runtime | Python 3.12+ |
| Packaging | uv, `pyproject.toml`, `src/` layout |
| LLM / embeddings | OpenAI Python SDK (`AsyncOpenAI`) — **no LangChain, no LlamaIndex** |
| Database | Postgres + pgvector, accessed via psycopg 3 async pool, **raw SQL** |
| Migrations | Alembic, dedicated `portfolio_rag` schema |
| API | FastAPI + Uvicorn |
| Config | pydantic-settings, all from env, validated at startup |
| Logging | structlog, JSON to stdout |
| Lint / format / types | ruff, mypy (strict) |
| Tests | pytest, pytest-asyncio; integration tests use a throwaway `pytest_*` schema, never `portfolio_rag` |
| Local dev | App on the dev machine via `uv run`; pgvector is a container on a LAN server, **port 5433** |
| Mail | `smtplib` (stdlib) via Gmail SMTP — app password, not the account password |
| CI/CD | GitHub Actions → GHCR; ruff, mypy and pytest gate the image build |
| Deploy | Docker Compose on an existing server: one image, `api` + `worker` services, `traefik_proxy` network |
| Repo | **Public on GitHub** — see the hygiene rules below |

## Layout

```
src/portfolio_ai/
  config.py      logging.py      strict_yaml.py (a repeated YAML key is an error)
  mail.py        (smtplib: one email; the digest and the contact form both send through it)
  db/            pool, documents, chat, feedback, analytics, evals  (raw SQL lives here only)
  llm/           client, embeddings, pricing, responses (the Responses API adapter)
  ingestion/     github, frontmatter, chunking, pipeline, cli
  assistant/     classifier, retrieval, agent (the loop), conversation (memory + storage),
                 postprocess (link filter, fallback), memory, cli, prompts/*.md + loader
  api/           main (app + lifespan), __main__ (the server), deps (admission), turns (answers in
                 flight), security, errors, schemas, middleware, routers/
  contact/       rules (n8n's, ported), classifier, reply, emails, service (the flow), desk
                 (the hour's count, messages in hand), replies (who may be written to), cli
  analytics/     retention, signals, report, digest, clustering, gaps, promote, seed, cli
  evals/         datasets, runner, judge, metrics, report, cli
migrations/      datasets/      tests/unit  tests/integration      docker/
```

## Conventions

- **Async throughout.** The API, DB access and OpenAI calls are all async. Only the CLI
  entrypoints use `asyncio.run`.
- **SQL lives in `db/`.** No queries in routers, pipelines or the agent. Always parameterised —
  never f-string a value into SQL.
- **Prompts live in `assistant/prompts/*.md`**, loaded at import, never inline string literals.
  Each prompt carries a version identifier that gets recorded in eval run configs.
- **Config only via `config.Settings`.** No `os.environ` reads scattered around the codebase.
- **Typed boundaries.** Pydantic models for anything crossing a boundary (HTTP, DB rows, LLM
  structured output). Plain dataclasses are fine for internal-only structures.
- **Errors:** raise domain exceptions from `portfolio_ai`; translate to HTTP status codes in one
  place, `api/errors.py` -- used both by the exception handlers and by the stream's `error`
  event, which is why it is not in `main.py`. Never leak an OpenAI or psycopg exception to a
  client response.
- **A turn outlives its request.** The API runs every answer in a task of its own
  (`api/turns.py`) and the endpoints only read its events from a queue, so a visitor who leaves
  mid-answer cancels the reading, not the answer -- it is still stored and counted against the
  limits. Never iterate `conversation.chat()` directly inside an endpoint.
- **A contact message outlives its request too, and 200 means the owner was emailed.** The
  endpoint awaits the flow's task through `asyncio.shield`, and `contact/desk.py` holds the
  task, so a caller that gives up between the two emails stops nothing. Everything before the
  owner's notification may fail and the message still goes (not classified, or no reply).
  **In any way**: those steps catch `Exception`, on purpose and nowhere else, because OpenAI
  answering 200 with a page of HTML is an `AttributeError` nobody lists. Only a notification
  that cannot be sent is an error.
- **Every refusal happens before the first byte.** Checks live in the admission dependency
  (`api/deps.py`), which takes the request body itself; the chat endpoints take nothing else,
  because FastAPI validates an endpoint's own parameters after its dependencies have run. The
  streaming endpoint never raises -- a failure after the first event is an `error` event.
- **Every OpenAI call records tokens, model and latency.** Cost visibility is a feature, not
  an afterthought.
- **Unit tests never read `.env`.** `tests/unit/conftest.py` switches the file off and gives
  every unit test the same fixed settings, so what passes locally is what CI runs. Before it
  existed, the agent tests passed on the dev machine by quietly reading the real `.env` and
  failed on the first push.
- Line length 100, ruff format, double quotes. Type hints on every public function.

## Domain facts to keep straight

- **Persona is "Rachel"**, the assistant *for* Mihail Mihaylov — never Mihail himself. Always
  third person about him. She does not introduce herself unless asked who she is.
- **Three-way classification** gates every message: `out_of_scope` (canned reply, no LLM spend),
  `small_talk` (short reply, no retrieval), `mihail_related` (full RAG). The classifier is given
  **the previous exchange** along with the new message. n8n gave it the message alone, which
  sent "yes please" -- the natural reply to Rachel's own offer of more detail -- to small talk,
  the one route that cannot search. That input change alone was not enough: golden_v1 still
  saw "yes please" go to small talk at the default effort. At `minimal`, five of eight questions
  about his projects that do not name him ("What is Glotsmith?") were refused as off-topic
  every time. Classifier
  prompt version 2 names his projects and says how to read a short reply.
  `portfolio-ai-validate` warns when a project article's title is missing from that list.
- **The first search is required**, not suggested: the knowledge-base route sends
  `tool_choice="required"` on its first call. The prompt already demands a search before any
  factual answer; this makes it so, and it is why every `mihail_related` answer has a
  `top_score`. Later rounds are the model's choice, up to `AGENT_MAX_SEARCH_ROUNDS`.
- **Search queries are rewritten** before retrieval: strip filler phrases and the name "Mihail",
  keep short keyword-style topical queries. This measurably improves retrieval.
- **Link rule:** never emit a URL containing `/knowledgebase/` or `/projects/`. Enforced both in
  the prompt and as a hard filter in code -- one that works on the token stream, holding back
  only the word in progress, so a forbidden URL never reaches a visitor even mid-answer. A
  bare one is swapped for the nearest real page (`https://mihaylov.io/#projects`, or the home
  page), both on the prompt's allowed list, so the sentence around it still reads.
- **Answer style:** 2–4 sentences, conversational, no section headers, no bullet lists unless
  asked. Offer more detail rather than dumping it.
- **Models, since 2026-10-10:** `gpt-6-luna` at effort `none` writes the answers, `gpt-5-mini` at
  `minimal` routes each message, and `gpt-6.1-sol` judges the evals. Until then `gpt-5-mini`,
  n8n's model, did the first two and `gpt-5` judged. A model and its effort go together:
  `gpt-6-luna`'s lowest is `none`, `gpt-5-mini`'s is `minimal`, OpenAI refuses each for the
  other, and so do the settings at startup. The knowledge base and the eval cases name none of
  these models, on purpose (Guardrails).
- **Carried over from n8n, unchanged:** embeddings `text-embedding-3-small` at 1536 dimensions,
  `top_k = 20`, memory window 25 **exchanges** (50 messages -- n8n's setting counts exchanges,
  and earlier drafts of these notes misread it as messages).
- **Frontmatter keys** in source docs: `doc_id`, `source_type`, `page_type`, `title`, `url`,
  `tags`, `last_verified`. `doc_id` is the stable identity used for upserts and purges.
- **Chunking** is one chunk per H2 section, chunk text = `"{section_title}\n\n{section_body}"`.
  A doc with no `##` headings becomes a single `main` chunk.
- **The corpus is small** — 11 Markdown files. Retrieval precision matters far more than scale,
  and `top_k = 20` is a large fraction of the whole corpus (worth testing a smaller value).
  Articles are authored as H2 questions, which is why section-level chunking works well; keep
  that convention when writing new ones.
- **Reasoning effort is the biggest lever on how the chat feels.** Same question, 2026-09-22:
  the n8n-parity default took 26.7s with the first word at 22.4s; `low` 10.6s/7.8s; `minimal`
  9.2s/5.9s, and cheaper each time. Measured on golden_v1 (2026-09-25, docs/EVALS.md):
  `minimal` brings the median first word from 15.6s to 3.9s and cuts the answer's cost by 45%,
  with no measurable loss in completeness or style and a small one in faithfulness. Each model
  now runs at the lowest effort it takes, `none` for `gpt-6-luna` and `minimal` for
  `gpt-5-mini`, and those are the code's defaults. The classifier counts as much as the chat:
  every message waits for it first, which is why it stayed on `gpt-5-mini` (about 0.8s a call)
  when the answers moved to `gpt-6-luna` (about 1.4s for the same call).
- **Analytics signals are recorded at answer time:** `top_score` (best cosine similarity),
  `fallback_used` (the "I do not have that information" answer, in the prompt's words or a
  rewording `postprocess.is_fallback` has been given) and `classification` go on every
  `chat_messages` row. The first and the last cannot be backfilled; `fallback_used` can be
  worked out again only while the answer's text is kept. Build these in with the API, not
  later. A reply marked as a fallback is also sent without its sources, which is the one
  place a visitor sees the signal. Each search also records, beside its chunk ids, the
  `doc_id` and section slug of every passage it returned (`tool_calls`). **Count by those,
  never by chunk id:** a chunk id is replaced whenever its article is indexed again, which is
  every time it is edited.
- **The feedback loop has a point:** low-score and fallback questions cluster into
  `content_gaps` rows, which become knowledge base articles, and the original question becomes
  an eval case via `analytics promote-case`. Failures turn into permanent regression tests.
- **`knowledgebase/**/*.md` is the only indexed source.** Case study pages and the site
  composables are deliberately excluded — `about-mihail.md` already covers the timeline, tech
  stack, education and contact details they hold. If the assistant cannot answer something, the
  fix is to **write a knowledge base article, never to widen the crawl.**
- **Rate limits:** 20 messages per session per 15 min (FastAPI), 60 per IP per 15 min (Express),
  one answer in flight per session (409), `MAX_CONCURRENT_TURNS` in flight overall (503), plus
  `DAILY_SPEND_CAP_USD` over a rolling 24 hours (default $1.00, about 1,600 answers) that returns
  a polite refusal rather than an error; `0` turns the chat off. The daily cap is the real
  protection; the others just stop casual abuse. The contact form has its own: 5 per IP per
  15 min (Express) and `CONTACT_MAX_PER_HOUR` (30) for the whole service, because the daily
  cap does not count its two model calls. Its automatic replies have two more: one per inbox
  a day, and `CONTACT_MAX_REPLIES_PER_DAY` (10) in all, which `0` switches off.
- **The contact form's rules are a port, held to n8n's own code.** `contact/rules.py` is the
  workflow's Code node line for line, its faults included: text between `<` and `>` is
  deleted, "Seoul" contains "seo", Cyrillic letters count as special characters
  (docs/CONTACT.md has the list). `tests/unit/fixtures/contact_rules.json` is what n8n's
  JavaScript said about 81 invented submissions, written by `contact_rules.js` from the
  gitignored export. **Fix a rule as a change of its own**, and say in it that the fixture
  then stops being n8n's verdict.
- **A blocked contact message is still delivered.** "Blocked" is a label in the owner's
  subject line, and no reply. The API answers 200 for it and the form says thank you: nobody
  is told they were taken for spam.
- **The acknowledgement is a model's text, sent to an address anyone could type.** It goes
  out only when the classifier allows it, is at least 0.6 sure of the reason and at most 0.3
  sure of spam, the rules found nothing suspicious, the address is one plain mailbox, and
  neither reply limit stops it (`contact/replies.py`). The model writes paragraphs. The code
  refuses a long reply, one that is not plain text, and one with a link, an address or a phone
  number in it. Everything else is fixed: the subject, the greeting, the signature, and the two
  lines under it. No code puts what a visitor typed into an email to a visitor; only the
  model's paragraphs, written from it, can echo any of it.
- **Retention is 90 days** for raw `chat_messages` and feedback. `content_gaps` and aggregates
  are kept indefinitely, so insight outlives the conversation. A promoted question leaves the
  database altogether: it is committed to this repository as an eval case.

## Public repo — hygiene

This repository is public on GitHub, and **git history is published too**. From the first commit:

- `.env` gitignored; `.env.example` carries placeholders only.
- **No personal email addresses in code or docs, except Mihail's two published ones**: the
  hiring address printed on his CV and the one for projects and general inquiries; the site
  lists both.
  They are public by his choice (confirmed 2026-09-24), and the golden datasets' contact cases
  check answers for them exactly. Nobody else's address goes in, least of all a visitor's in a
  question promoted from real traffic; a unit test checks every dataset for that. The digest's
  recipient comes from `DIGEST_TO_EMAIL` and the contact form's from `CONTACT_TO_EMAIL`,
  because they are configuration.
- **No IPs or hostnames in committed files** — not the LAN dev server, not the EC2 host, not
  container names. They live in `.env`. `docker/docker-compose.yml` is a template, and `mihaylov.io` is
  the only host that should appear anywhere in the repo.
- Prompts, schema and eval datasets are fine to publish — they are much of the value.
- **Real chat logs never leave the database.** `datasets/` holds curated cases only; read any
  question promoted from real traffic before committing it.

## Commands

These are the intended interfaces; add them as each phase lands.

Everything runs **on the dev machine**, against the pgvector container on the user's LAN server
(**port 5433** — 5432 on the same host is plain Postgres with no pgvector). Do not add a Postgres
service to a compose file for development, and do not run the app in Docker to test a change —
`--reload` is the loop.

```bash
uv sync                                          # install
uv run ruff check . && uv run ruff format .      # lint / format
uv run mypy                                      # types: src, tests and migrations
uv run pytest                                    # unit; -m integration needs Docker running

uv run alembic upgrade head                      # migrations (creates the portfolio_rag schema)

uv run portfolio-ai-validate ../my-portfolio/knowledgebase   # check articles; no DB, no network

uv run python -m portfolio_ai.ingestion --dry-run    # plan only, no DB writes, no spend
uv run python -m portfolio_ai.ingestion              # incremental ingest
uv run python -m portfolio_ai.ingestion --force      # re-embed everything

uv run python -m portfolio_ai.assistant               # chat with Rachel in the terminal
uv run python -m portfolio_ai.assistant --no-save -v  # store nothing; show what searches found
uv run python -m portfolio_ai.assistant -m "..."      # ask one question and exit

uv run uvicorn portfolio_ai.api.main:app --reload     # dev API; /docs for the schema
uv run python -m portfolio_ai.api                     # the API as production runs it

uv run python -m portfolio_ai.analytics purge --dry-run   # retention sweep, counting only

uv run python -m portfolio_ai.evals check datasets/golden_v3.yaml     # no DB, no network
uv run python -m portfolio_ai.evals run --label mini-answers \
    --chat-model gpt-5-mini --chat-effort minimal --dry-run          # the plan; spends nothing
uv run python -m portfolio_ai.evals run --label mini-answers \
    --chat-model gpt-5-mini --chat-effort minimal                    # a model and its effort
uv run python -m portfolio_ai.evals show mini-answers --failures
uv run python -m portfolio_ai.evals compare v3-baseline mini-answers          # a change of model
uv run python -m portfolio_ai.evals rejudge v3-baseline --label v3-baseline-regraded --dry-run   # a new judge

uv run python -m portfolio_ai.analytics report --since 7d   # quotes visitors: terminal only
uv run python -m portfolio_ai.analytics seed --dry-run      # invented chats: the plan
uv run python -m portfolio_ai.analytics seed                # about $0.10; local only
uv run python -m portfolio_ai.analytics seed --clear        # take them out again
uv run python -m portfolio_ai.analytics digest --dry-run    # the weekly email, printed
uv run python -m portfolio_ai.analytics digest --test       # one line, to prove the mail settings
uv run python -m portfolio_ai.analytics refresh-gaps --dry-run   # the content gaps: the plan
uv run python -m portfolio_ai.analytics refresh-gaps        # names new gaps; a cent or less
uv run python -m portfolio_ai.analytics gaps                # the writing queue; quotes visitors
uv run python -m portfolio_ai.analytics gap-status 7 written --doc services
uv run python -m portfolio_ai.analytics promote-case 812 --expected-doc services   # quotes visitors

uv run python -m portfolio_ai.contact try --message "..."   # the form's two emails, printed; sends nothing
node tests/unit/fixtures/contact_rules.js                    # n8n's verdicts again; needs the export
```

Production schedules live in `docker/crontab`, run by the `worker` container with
`TZ=Europe/London`: ingest hourly on the hour, purge daily at 03:00, and the digest on Mondays
at 05:30. Ingest is hourly rather than daily because a run that finds nothing changed costs
nothing -- it compares git blob SHAs and stops. The digest sends nothing, and exits cleanly,
in a week when nobody asked anything and while the mail settings are not in `.env`.

## Guardrails

- **Two environments, two machines.** Local is a LAN server holding nothing live — purge, drop
  and re-ingest there freely. Production is an EC2 instance that also runs the live site, n8n and
  n8n's `mihaylov_rag_documents` table. **Never point a dev
  command at production**; check which `DATABASE_URL` is loaded before anything that writes.
- **Port 5433, not 5432.** Only 5433 has pgvector. A wrong port connects fine and then fails at
  the first migration with `type "vector" does not exist`, which reads like a broken migration
  rather than a wrong host.
- **Tests must never touch the `portfolio_rag` schema.** Integration tests create their own
  `pytest_*` schema on the same instance, migrate into it, and drop it. A test that reads or
  writes `portfolio_rag` is a bug, not a shortcut. The fixture refuses to run unless
  `ENVIRONMENT` is `local`, because it drops schemas.
- **The pgvector container is the user's, not this project's.** Do not add a Postgres service to
  a dev compose file, do not run migrations that drop anything outside `portfolio_rag`, and do
  not assume `CREATE EXTENSION vector` has been run — the first migration should fail loudly if
  it has not.
- **The database is across the network, not on a local socket.** Batch chunk inserts with
  `execute_many` or `COPY` rather than looping single statements; the round-trips are visible
  here in a way they would not be on localhost.
- **The purge step is guarded by a share, not a count.** A run may delete at most
  `INGESTION_MAX_PURGE_FRACTION` of what is stored (plus an `INGESTION_PURGE_GRACE` floor so a
  tiny corpus is not frozen); beyond that it aborts. A fixed floor was tried first and rejected —
  it stops protecting as the corpus grows, since a floor of 8 guards 11 documents and guards
  nothing at 50. Checked twice: optimistically after discovery, before anything is fetched or
  spent, and exactly at the purge itself.
- **n8n's `mihaylov_rag_documents` table is not this project's to touch.**
  `mihaylov_chat_histories` went on 2026-10-13, with the chat workflows. The documents table
  stays until the new ingestion has a month of clean runs behind it, about 9 November, and
  dropping it is the owner's step (docs/DEPLOYMENT.md §12, step 7). In production it lives in
  n8n's database; this project has its **own database**, `portfolio_ai`, on the same Postgres
  instance, with the `portfolio_rag` schema inside it. Never point this project at n8n's
  database. (Locally, `portfolio_rag` shares a database with other things —
  the schema is what isolates it there, and production is stricter.)
- **n8n itself stays, and almost none of it is this project's.** It runs the owner's other
  workflows; touch none of them. The contact form's was deactivated on 2026-10-14 and is
  kept, never deleted: it is the rollback.
- **Do not commit secrets.** `OPENAI_API_KEY`, `DATABASE_URL`, `PORTFOLIO_AI_API_KEY` and
  `SMTP_APP_PASSWORD` come from `.env`, which is gitignored. The repo is public, so a leaked
  secret is leaked to everyone, permanently, in history.
- **The terminal chat will not write to production.** `python -m portfolio_ai.assistant`
  refuses to run with `ENVIRONMENT=production` unless `--no-save` is given, because a smoke
  test typed into the chat tables is indistinguishable from a visitor afterwards.
- **No test chats through the production API.** Every turn it answers is stored. Check a
  deployment with the non-writing calls in docs/DEPLOYMENT.md §8 (health, readiness, a 401, a
  feedback 404) and check OpenAI with the terminal chat's `--no-save`.
- **`analytics report`, `analytics gaps` and `analytics promote-case` print what visitors
  typed.** Run them in a terminal and read them there. Never put one in the crontab, where its
  output would be the worker's log. Never paste what `report` or `gaps` prints anywhere; what
  `promote-case` prints has one destination, below, and only after it is rewritten. On the
  server it is `docker compose exec`, whose output goes to the terminal and not to the log.
  What may leave the database's side is the other form, `report.render(verbatim=False)`:
  counts, article titles and the gaps' labels, with a unit test that searches it for every
  question, answer and comment.
- **A promoted case is rewritten before it is committed.** The chat tells visitors their
  conversations are deleted after 90 days, and a commit to a public repository is for good.
  So the question, and any history, is put into other words: what is asked is kept, the
  visitor's phrasing and anything that says who asked is not. The unit tests catch an email
  address and nothing else. The case goes into `datasets/promoted.yaml`, which is checked and
  **never run**: `evals run` refuses the name, because a complete run would freeze a file that
  exists to be added to. Its cases join the next golden dataset, reviewed with the rest
  before that dataset's first run.
- **A redirect is not a copy.** `promote-case` writes UTF-8, and Command Prompt's `>` keeps
  it. Windows PowerShell's `>` and `|` decode a program's output with the console's code page
  and write UTF-16: the file holds other characters, with exit code 0. Copy from the terminal
  there.
- **A unit test of an analytics command replaces every database call the command makes.**
  When `digest` learnt to refresh the gaps, its tests reached for a database that is not
  there and the unit suite went from eleven seconds to two and a half minutes, passing or
  failing on a timeout. Watch the suite's duration after adding a call to a command.
- **The digest's log lines carry counts and never an address.** Who it is sent to is
  personal data like a question is, and under the crontab a log line is all the command
  prints. `mail.send` raises errors that give the kind of failure and not the server's words,
  which quote the addresses involved.
- **A contact form message is stored nowhere and logged nowhere.** There is no table. It
  exists in the request, in the two calls to OpenAI and in the two emails, and the emails are
  the record. `contact_handled` carries counts, flags and the rules' fixed wording, and a
  reason only when it is one of the form's five words. A table of messages would need
  everything the chat's has: retention, the purge, and a notice on the site.
- **A mail header is what the library makes of the text, not the text.** `EmailMessage`
  decodes encoded words (`=?utf-8?q?...?=`) in any header it is handed, and refuses whatever
  `str.splitlines` splits on, U+0085 included. An address that read as one mailbox was sent to
  two, and a subject with U+0085 lost the message, until the review sent some. So what a
  visitor typed goes through `emails.header_text` on its way into a subject, an address
  through `emails.safe_recipient`, and `mail._check_headers` compares the built message with
  what was asked for. **A test of a header reads `server.delivered`**, the envelope and the
  bytes `smtplib` would hand a server, and never the `EmailMessage` object.
- **No unit test can reach a mail server.** `tests/unit/conftest.py` replaces `smtplib.SMTP`
  for every test, and a test that sends installs `tests/smtp_fake.py` over it. Before the
  guard, one test of the contact flow written without the stand-in opened a connection to the
  real server in the default settings. Nothing was sent.
- **No command sends a contact email.** `contact try` prints both and sends neither, and it
  spends a fraction of a cent. The real path is tested through the site's form, by the owner
  (docs/DEPLOYMENT.md §11, "The contact form").
- **A gap's label is a model's output, and is treated as input.** It is written from what
  visitors typed, kept for good, and printed in a terminal and an email. `gaps._fault` refuses
  one that is over 60 characters, is not plain text or holds a link or an address, and the
  model is shown one clipped line of each question (`gaps.brief`). A label is in the email and
  never in a log line. Anything else a model writes that is stored or printed gets the same
  treatment.
- **Test what is not logged with a recorder, not with structlog's `capture_logs()`.** A logger
  is cached at its first use, and one cached before a later `configure_logging` is not
  captured, so the test passes on an empty list. `tests/log_recorder.py` is patched onto the
  module, and the test first asserts that the lines it expects were recorded at all.
- **Invented conversations go into the development database only.** `analytics seed` refuses
  any other `ENVIRONMENT`, its dry run included. A stored turn does not say where it came
  from, so in production an invented one would be counted as a visitor for good. Locally they
  are told apart by a session id starting `invented-`, and `seed --clear` removes them by it.
- **Embedding dimension changes are migrations.** Changing `EMBEDDING_DIMENSIONS` or the
  embedding model invalidates every stored vector and requires a full re-embed.
- **Evals cost money.** A full run of the golden dataset is about $0.55, nearly all of it the
  `gpt-6.1-sol` judge (`--no-judge` about 4 cents). Say what a run or a sweep will cost before
  starting it.
- **Evals run locally, against the dev database.** `run` and `rejudge` refuse
  `ENVIRONMENT=production`. A run inherits whatever the dev `.env` sets, so read the dry run's
  configuration first, and give a model and its effort together.
- **The judge is the ruler.** Changing `JUDGE_MODEL` or `prompts/judge.md` makes every new score
  incomparable with every stored one. Grade the stored baseline again first (`evals rejudge`),
  and compare new runs with that. `compare` leaves the judge's scores out when two runs had
  different judges.
- **Measure a prompt wording with repetitions before a full run**, and change one line at a
  time. At the lowest effort a rule aimed at one case leaks into others, and naming a phrase to
  avoid can bring it out (docs/EVALS.md, "The chat prompt, version 2").
- **golden_v3 is the current dataset, and the default.** golden_v1 describes the knowledge base
  before the cutover, and golden_v2 the one that named its models. Both are kept for their
  runs, and runs of different datasets cannot be compared.
- **Nothing an answer is graded against states what the evals exist to change.** That is
  whatever `evals run` takes an option for (the models, their efforts, `top_k`, the number of
  search rounds), and with those the judge and the dataset's size. Not in a knowledge base
  article and not in a case. golden_v2 pinned the answer model through one line of one article
  and one required phrase, and the first change of model cost a dataset version and a
  baseline. Only a model's name has a check: `portfolio-ai-validate` warns about one in an
  article (a warning, which CI does not fail on), and a unit test fails on one in a case. The
  rest is found by reading, so read an article and a reference against every `evals run`
  option. A change of model is then one `run`, one `compare`, and one line of README.md,
  which names the models in use and is where the article sends a curious visitor.
- **Have a dataset reviewed before its first run, not after.** A complete run freezes it, and
  a note freezes with its case. golden_v2 was reviewed after its baseline and its faults
  waited for golden_v3; golden_v3 was reviewed before, and seven more cases changed.
- **A dataset freezes once a run of it completes.** Scores are only comparable on identical
  cases, so a changed golden_v3.yaml is refused after its first complete run -- copy it to
  golden_v4 instead, and make that the default in `evals/cli.py`. Keep a draft under another
  name (`draft_v4.yaml`) until it is reviewed: a file called `golden_v4.yaml` is what a unit
  test then demands as the default, and the default is what a run without `--dataset` uses.
  Never run a dataset still under review, even one case of it: a complete `--only` run freezes
  it too. Smoke-test the harness on a throwaway dataset file.
- **Ask before changing the tuned prompts.** They came from a working production system; changes
  should be justified by an eval run, not by taste. The contact form's prompts are in the same
  position: n8n's wording, with every change listed in the file's own frontmatter. No eval
  covers them, so a change is read with `contact try`, several times over.
- **Chat logs are real visitors' words.** People volunteer identifying details in a chat box
  ("I'm hiring for a Laravel role at Acme"). IPs are stored only as an HMAC keyed with
  `IP_HASH_SALT`, `CHAT_RETENTION_DAYS` is enforced nightly, nothing a visitor types goes into a
  log line, never publish the digest, and do not paste raw conversation content into anything
  external.

## Current state

Steps 1-5 of the build order (ARCHITECTURE.md §12) are done, the cutover in step 6
happened on 2026-10-09, and **every open requirement question is closed** (the decisions log is
ARCHITECTURE.md §13).

- **Foundations** — packaging, settings, logging, the async pool, migrations, tests, CI and the
  image on GHCR. Written up as ten lessons in `docs/lessons/`.
- **Ingestion** — deployed and running hourly: 11 documents, and 123 chunks since the assistant's
  article gained a section on 2026-10-13 (122 after the cutover, 117 before it).
- **Assistant core** — Rachel answers end to end from the terminal: classify, search, answer,
  remember, record.
- **API** — FastAPI over the assistant, private to the Docker network: bearer auth, per-session
  and global limits, the daily spend cap, answers as JSON or streamed as server-sent events, the
  feedback endpoint, and the nightly retention purge. The portfolio's Express routes that call it
  went live at cutover.

- **Evals** — `python -m portfolio_ai.evals`: a golden dataset, deterministic retrieval and rule
  checks, a judge (`gpt-6.1-sol`, rubric version 2), and runs stored with their full
  configuration for comparison.
  golden_v1 (54 cases from the eleven articles before the cutover) is frozen: the `gpt-5-mini`
  baseline at n8n's settings (parity by construction) and runs at chat effort `low` and `minimal`
  are in docs/EVALS.md.

  Those results put production at `minimal`. Classifier prompt version 2 fixed the misroutes
  the runs found (`classifier-v2`).

  golden_v2 (62 cases, from the articles after the cutover) is frozen too. `v2-baseline` is
  `gpt-5-mini` at production's settings of 2026-10-09, and `v2-baseline-sol` is the same
  answers graded by the current judge.

  2026-10-10 changed three things, each measured (docs/EVALS.md): the chat prompt went to
  version 2, the judge to `gpt-6.1-sol` with rubric version 2, and the answers to `gpt-6-luna`.
  `luna-mini` is the run of the settings adopted, on golden_v2: `gpt-6-luna` answering,
  `gpt-5-mini` routing, the final prompt. Every route right, and faithfulness 4.94,
  completeness 4.13 and style 4.89 against `v2-baseline-sol`'s 4.82 / 4.19 / 4.39, at a third
  of the cost per answer and with the median first word at 3.2s against 2.8s. `luna-v3`, the
  run the decision was made on, had `gpt-6-luna` routing as well. The owner deployed the model
  change on 2026-10-10 (docs/DEPLOYMENT.md §9, which also says what an image rollback needs
  first). The wider fallback match was committed after that deploy and reaches production
  with the next one.

  **golden_v3 (2026-10-13) is the current dataset, and the first that names no model.**
  golden_v2 required an answer to name the model that wrote it, because the assistant's
  article named it. The change of model made the article false, and correcting it would have
  made the case false. The knowledge base now states no model, effort setting, search-round
  limit, judge or dataset size, and golden_v3 is golden_v2 with sixteen cases corrected, seven
  of them found by the review before its first run (docs/EVALS.md). **`v3-baseline` is its
  baseline, and what the next change is compared with**: the settings in use (`gpt-6-luna`
  answering, `gpt-5-mini` routing), every route right, faithfulness 4.98, completeness 4.17,
  style 4.89, the first word at 3.2s median and $0.0006 an answer. No answer names a model.
  Its figures cannot be set beside golden_v2's: sixteen cases and two articles differ.

**Step 6**, the front end and cutover, is in phases:
1. the Express proxy in the portfolio repo: built, with tests, and switched on at cutover;
2. the new chat UI: built, with unit tests for its logic, and live. Its answer renderer ports
   `postprocess.py`'s link pattern, checked against a fixture of Python's matches;
3. the rewritten assistant article and the other copy that became false at cutover, the project
   card's image and the CV included: live;
4. cutover itself: done on 2026-10-09, by the runbook in docs/DEPLOYMENT.md §12. The owner
   switched n8n's chat workflows off on 2026-10-13 (step 7 there);
5. golden_v2 and its baseline: done on 2026-10-09.

Still open from the evals:
- "tell me everything" runs long on every model and wording tried;
- completeness is the score with room in it on golden_v3 (4.17): twelve answers at 3, each
  giving the main point and leaving out one to three details of its reference. It is where a
  comparison of models will show.

**Step 7, analytics reporting, is built, in four stages and without visitors.** It was
planned for after the site had traffic. It has none, and none is expected for months, so it was
built against 28 invented conversations (`datasets/traffic/invented.yaml`) that `analytics seed`
plays through the real assistant on the development database (docs/ANALYTICS.md). The
*capture* it reads (feedback, `top_score`, `fallback_used`, where a conversation came from)
shipped with the API, because none of it can be reconstructed after the fact.

1. the report and the invented conversations: built (`analytics report`, `analytics seed`);
2. the weekly email, with counts and nothing a visitor typed: built (`analytics digest`,
   sent with `smtplib` after the event loop has finished). The owner set its mailbox up on
   the server on 2026-10-13 (docs/DEPLOYMENT.md §11);
3. content gaps: built (`refresh-gaps`, `gaps`, `gap-status`, migration 0006). The failed
   questions are clustered **by the search each answer ran, not by the question as typed**,
   which grouped them by phrasing and by the word "Mihail". Each gap without a name is named
   by the classifier's model from `prompts/gap_label.md`, and `digest` refreshes them before
   it builds the report, in a quiet week too. `content_gaps` keeps the ids of example
   questions, never their text. One refresh groups at most the newest 500 failed questions
   and names at most twenty gaps. **A gap's count of fallbacks does not say whether it is a
   real gap**: read its questions with `gaps` before marking it `wont_fix`;
4. `promote-case`: built (migration 0007). It prints a visitor's question as a YAML eval
   case, built as the `EvalCase` the harness loads, for a holding file
   (`datasets/promoted.yaml`, checked and never run) until the next golden dataset. It stores
   nothing: eval cases live in the development database and visitors' messages in
   production's, which is also why `eval_cases.source_message_id` could never be filled and
   was dropped. The holding file does not exist until the first real promotion.

With that every step of the original build order is built.

**Step 8, the contact form, is live since 2026-10-14** (docs/CONTACT.md). It was
not in the plan: once the chat had moved, the form's workflow was the last thing the site
called n8n for. Here: `contact/` (the ported rules, the classifier, the reply, the emails, the
flow, the desk), `POST /v1/contact`, `mail.py` moved up out of `analytics/`, eight prompts, and
`python -m portfolio_ai.contact try`. In the portfolio: `api/src/contact.js` hands each message
to this service, and the form's three typed fields have a `maxlength`. The owner deployed
both, deactivated the n8n workflow and took its settings out of `.env` on 2026-10-14
(docs/DEPLOYMENT.md §12). **A message sent through the form had not been reported when this
was written**: it is the check that the switch works, step 4 of that runbook.

What its trial showed (2026-10-13), on eight invented submissions: one of each of the five
reasons was accepted and acknowledged, and a sales pitch, an attempt to dictate the reply and a
one-line "seo role" were blocked by the classifier. About $0.0008 for a message that is
acknowledged. **How long the form waits has not been measured**: the first real message's
`request` line is the measurement. The rules' faults are listed in docs/CONTACT.md and left
alone on purpose.

The image takes `python:3.12-slim-bookworm` at whatever patch level it is built on, and the
development machine runs 3.12.3. By the review's reading of CPython's source, a fix to the
mail library sits between that and later 3.12 releases, so the two can differ in how a header
is written out. The header checks above do not depend on it.

Since then, the replies are hardened against use of the form to write to other people
(2026-10-14): one per inbox a day, ten a day in all, a fixed subject, no phone numbers, and a
line for whoever never wrote. The worst case went from about 720 unwanted emails a day to ten.

What is left besides is not building: that message through the form, dropping
`mihaylov_rag_documents` in November, and setting the thresholds again once there are visitors.

What the first seeding showed (2026-10-13):
- **`fallback_used` is a floor.** On 26 new questions the knowledge base cannot answer it was
  set on 11. The other 15 answers declined in words the phrase match does not know. The
  match's 41 of 43 on the eval answers was over the same handful of questions asked in every
  run.
- **`LOW_SCORE_THRESHOLD` (0.35), `REPHRASE_SIMILARITY` (0.60) and `GAP_SIMILARITY` (0.45) are
  provisional**, set from invented questions and eval cases. Every report ends with the
  distributions to set the first two again from. A low score is not a bad answer: "Who is
  Mihail anyway?" scores 0.23 and is answered well.
