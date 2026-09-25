# CLAUDE.md

Context for Claude Code working in this repository. Read [ARCHITECTURE.md](ARCHITECTURE.md) for
the full design; this file is the short version plus the working agreements.
[docs/INGESTION.md](docs/INGESTION.md), [docs/ASSISTANT.md](docs/ASSISTANT.md),
[docs/API.md](docs/API.md) and [docs/EVALS.md](docs/EVALS.md) walk through the code of the four
built flows, module by module -- update them in the same change as the code they describe.
API.md also holds the contract the Express proxy has to meet in step 6.

## What this project is

A production Python system that replaces two n8n workflows powering the AI chat box on
[mihaylov.io](https://mihaylov.io), plus an eval harness and an analytics loop. Four deliverables:

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

The exported n8n workflows are the functional spec for deliverables 1 and 2. They sit in
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

Current state there, as of 2026-09-25: the chat is still the stock `@n8n/chat` widget posting
straight from the browser to n8n (unauthenticated); the site is fully prerendered with no Nitro
runtime in production; session id lives in `localStorage["n8n-chat/sessionId"]`. The Express API
now has `/api/chat` and `/api/chat/feedback` (step 6), which proxy to this service and stay
switched off until `PORTFOLIO_AI_URL` is set; nothing on the site calls them yet. **The old widget
has no feedback**, despite the README claiming thumbs up/down exists. The `projects/portfolio-ai-assistant.md` knowledge base
article made the same claim, and an Express proxy and reCAPTCHA besides, until a fact-check of
every article on 2026-09-23 removed them; at cutover it is rewritten to describe the new system.

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
  `localStorage`; `mihaylov_chat_histories` is dropped at cutover.
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
  config.py      logging.py
  db/            pool, documents, chat, feedback, analytics, evals  (raw SQL lives here only)
  llm/           client, embeddings, pricing, responses (the Responses API adapter)
  ingestion/     github, frontmatter, chunking, pipeline, cli
  assistant/     classifier, retrieval, agent (the loop), conversation (memory + storage),
                 postprocess (link filter, fallback), memory, cli, prompts/*.md + loader
  api/           main (app + lifespan), __main__ (the server), deps (admission), turns (answers in
                 flight), security, errors, schemas, middleware, routers/
  analytics/     retention, cli; later signals, clustering, digest
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
- **Defaults carried over from n8n:** chat model `gpt-5-mini`, embeddings `text-embedding-3-small`
  at 1536 dimensions, `top_k = 20`, memory window 25 **exchanges** (50 messages -- n8n's
  setting counts exchanges, and earlier drafts of these notes misread it as messages).
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
  with no measurable loss in completeness or style and a small one in faithfulness. Production's
  `.env` now sets `minimal` for both the chat and the classifier; the code default stays at
  parity. `CHAT_REASONING_EFFORT` and `CLASSIFIER_REASONING_EFFORT` in `.env` change it.
- **Analytics signals are recorded at answer time and cannot be backfilled:** `top_score`
  (best cosine similarity), `fallback_used` (the "I do not have that information" answer) and
  `classification` go on every `chat_messages` row. Build these in with the API, not later.
- **The feedback loop has a point:** low-score and fallback questions cluster into
  `content_gaps` rows, which become knowledge base articles, and the original question becomes
  an eval case via `analytics promote-case`. Failures turn into permanent regression tests.
- **`knowledgebase/**/*.md` is the only indexed source.** Case study pages and the site
  composables are deliberately excluded — `about-mihail.md` already covers the timeline, tech
  stack, education and contact details they hold. If the assistant cannot answer something, the
  fix is to **write a knowledge base article, never to widen the crawl.**
- **Rate limits:** 20 messages per session per 15 min (FastAPI), 60 per IP per 15 min (Express),
  one answer in flight per session (409), `MAX_CONCURRENT_TURNS` in flight overall (503), plus
  `DAILY_SPEND_CAP_USD` over a rolling 24 hours (default $1.00, about 250 answers) that returns
  a polite refusal rather than an error; `0` turns the chat off. The daily cap is the real
  protection; the others just stop casual abuse.
- **Retention is 90 days** for raw `chat_messages` and feedback. `content_gaps`, aggregates and
  promoted `eval_cases` are kept indefinitely, so insight outlives the conversation.

## Public repo — hygiene

This repository is public on GitHub, and **git history is published too**. From the first commit:

- `.env` gitignored; `.env.example` carries placeholders only.
- **No personal email addresses in code or docs, except Mihail's two published ones**: the
  hiring address printed on his CV and the one for projects and general inquiries; the site
  lists both.
  They are public by his choice (confirmed 2026-09-24), and golden_v1's contact cases check
  answers for them exactly. Nobody else's address goes in, least of all a visitor's in a
  question promoted from real traffic; a unit test checks every dataset for that. The digest
  recipient still comes from `DIGEST_TO_EMAIL`, because it is configuration.
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

uv run python -m portfolio_ai.evals check datasets/golden_v1.yaml     # no DB, no network
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label baseline \
    --chat-effort default --classifier-effort default --dry-run      # the plan; spends nothing
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label effort-low \
    --chat-effort low --classifier-effort default   # one setting changed from the baseline
uv run python -m portfolio_ai.evals show baseline --failures
uv run python -m portfolio_ai.evals compare baseline effort-low

uv run analytics report --since 7d                    # digest to stdout
uv run analytics digest                               # build + email it (Gmail SMTP)
uv run analytics gaps --status open                   # ranked content-writing queue
uv run analytics promote-case <message_id> --expected-doc <doc_id>
```

Production schedules live in `docker/crontab`, run by the `worker` container with
`TZ=Europe/London`: ingest hourly on the hour, purge daily at 03:00, and -- once step 7 builds
it -- the digest on Mondays at 05:30 (commented out until then). Hourly rather than daily because a run that finds nothing changed costs nothing --
it compares git blob SHAs and stops.

## Guardrails

- **Two environments, two machines.** Local is a LAN server holding nothing live — purge, drop
  and re-ingest there freely. Production is an EC2 instance that also runs the live site, n8n and
  the live `mihaylov_rag_documents` / `mihaylov_chat_histories` tables. **Never point a dev
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
- **The n8n tables (`mihaylov_rag_documents`, `mihaylov_chat_histories`) are live production.**
  In production they live in n8n's database; this project has its **own database**,
  `portfolio_ai`, on the same Postgres instance, with the `portfolio_rag` schema inside it.
  Never point this project at n8n's database, and do not touch or drop the old tables until
  cutover is explicitly confirmed. (Locally, `portfolio_rag` shares a database with other things —
  the schema is what isolates it there, and production is stricter.)
- **Do not commit secrets.** `OPENAI_API_KEY`, `DATABASE_URL`, `PORTFOLIO_AI_API_KEY` and
  `SMTP_APP_PASSWORD` come from `.env`, which is gitignored. The repo is public, so a leaked
  secret is leaked to everyone, permanently, in history.
- **The terminal chat will not write to production.** `python -m portfolio_ai.assistant`
  refuses to run with `ENVIRONMENT=production` unless `--no-save` is given, because a smoke
  test typed into the chat tables is indistinguishable from a visitor afterwards.
- **No test chats through the production API.** Every turn it answers is stored. Check a
  deployment with the non-writing calls in docs/DEPLOYMENT.md §8 (health, readiness, a 401, a
  feedback 404) and check OpenAI with the terminal chat's `--no-save`.
- **Embedding dimension changes are migrations.** Changing `EMBEDDING_DIMENSIONS` or the
  embedding model invalidates every stored vector and requires a full re-embed.
- **Evals cost money.** A full golden_v1 run is about $0.75, most of it the `gpt-5` judge
  (`--no-judge` about $0.20). Say what a run or a sweep will cost before starting it.
- **Evals run locally, against the dev database.** `run` refuses `ENVIRONMENT=production`. The
  dev `.env` sets both reasoning efforts to `low`, so a run meant to match production passes
  `--chat-effort default --classifier-effort default`; read the dry run's configuration first.
- **A dataset freezes once a run of it completes.** Scores are only comparable on identical
  cases, so a changed golden_v1.yaml is refused after its first complete run -- copy it to
  golden_v2 instead. Never run a dataset still under review, even one case of it: a complete
  `--only` run freezes it too. Smoke-test the harness on a throwaway dataset file.
- **Ask before changing the tuned prompts.** They came from a working production system; changes
  should be justified by an eval run, not by taste.
- **Chat logs are real visitors' words.** People volunteer identifying details in a chat box
  ("I'm hiring for a Laravel role at Acme"). IPs are stored only as an HMAC keyed with
  `IP_HASH_SALT`, `CHAT_RETENTION_DAYS` is enforced nightly, nothing a visitor types goes into a
  log line, never publish the digest, and do not paste raw conversation content into anything
  external.

## Current state

Steps 1-5 of the build order (ARCHITECTURE.md §12) are done and step 6 is under way, and
**every open requirement question is closed** (the decisions log is ARCHITECTURE.md §13).

- **Foundations** — packaging, settings, logging, the async pool, migrations, tests, CI and the
  image on GHCR. Written up as ten lessons in `docs/lessons/`.
- **Ingestion** — deployed and running hourly: 11 documents, 117 chunks as of 2026-09-25.
- **Assistant core** — Rachel answers end to end from the terminal: classify, search, answer,
  remember, record.
- **API** — FastAPI over the assistant, private to the Docker network: bearer auth, per-session
  and global limits, the daily spend cap, answers as JSON or streamed as server-sent events, the
  feedback endpoint, and the nightly retention purge. The portfolio's Express routes that call it
  are built (step 6) and deploy switched off until cutover.

- **Evals** — `python -m portfolio_ai.evals`: golden_v1 (54 cases from the eleven articles),
  deterministic retrieval and rule checks, a `gpt-5` judge, and runs stored with their full
  configuration for comparison. golden_v1 is reviewed and frozen: the `gpt-5-mini` baseline at
  n8n's settings (parity by construction) and runs at chat effort `low` and `minimal` are in
  docs/EVALS.md.

  Production's `.env` sets both efforts to `minimal`, on those results; the code default stays at
  n8n parity. Classifier prompt version 2 fixed the misroutes the runs found (`classifier-v2`).

**Step 6**, the front end and cutover, is in phases:
1. the Express proxy in the portfolio repo: built, with tests, deployable switched off;
2. the new chat UI;
3. the rewritten assistant article and the other copy that becomes false at cutover;
4. cutover itself, by the runbook in docs/DEPLOYMENT.md §12;
5. then golden_v2.

After that, analytics reporting (7). The chat prompt's weak cases at `minimal` (the end of Results
in docs/EVALS.md) are worth measuring whenever convenient.

Analytics reporting is last on purpose — it needs real traffic to be worth writing. The
*capture* (feedback, `top_score`, `fallback_used`, where a conversation came from) shipped with
the API, because none of it can be reconstructed after the fact.
