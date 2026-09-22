# Portfolio AI Assistant — Architecture & Design

Python replacement for the two n8n workflows that currently power the AI chat box on
[mihaylov.io](https://mihaylov.io), plus an evaluation harness and an analytics/feedback loop.

**Status:** design agreed, implementation not started.
**Last updated:** 2026-09-20

---

## 1. Goals

| # | Deliverable | Runs how |
|---|-------------|----------|
| 1 | **Ingestion** — read Markdown docs from a GitHub repo, chunk, embed, upsert into Postgres/pgvector | Scheduled daily in production; manually on demand |
| 2 | **Assistant** — RAG chat API answering questions about the documentation | Authenticated HTTP endpoint called by the portfolio site |
| 3 | **Evals** — measure assistant quality across models/configs | Ad-hoc CLI, results persisted for comparison |
| 4 | **Analytics & feedback** — track what is asked, what fails, and what the docs are missing | Captured continuously, reported weekly |

Secondary goal, explicitly stated by the owner: **learn Python and Python-for-AI properly.**
That biases every choice toward transparent, readable code over framework magic.

### Non-goals

- Multi-tenant / multi-user support. One knowledge base, one site.
- Replacing n8n entirely — only these two workflows.
- Real-time (webhook-on-push) ingestion. Daily is sufficient; the repo changes rarely.
- A live analytics dashboard. See §10 — traffic does not justify it yet.

---

## 2. What we are replacing

Two exported n8n workflows are the functional specification — the Python version must reach
parity before cutover. They live in `n8n_workflows/` locally but are **gitignored**: the exports
carry the n8n instance id, credential ids and webhook ids, which are internal identifiers with no
reason to be in a public repository. Everything needed from them is transcribed below, which is
why this section is as detailed as it is.

### 2.1 `Portfolio | Knowledgebase -> RAG Vector Store`

Triggers: manual, plus schedule (currently **Mondays 06:00** — the new system moves to **daily**).

```
GitHub tree API (recursive, branch main)
  |- filter: blob && path startsWith "knowledgebase/" && path endsWith ".md"
      |- fetch raw.githubusercontent.com/<path>
          |- parse YAML-ish frontmatter -> {body, metadata}
              |- [per doc] DELETE FROM mihaylov_rag_documents WHERE metadata->>'doc_id' = $1
              |- split body on "## " H2 headings -> one chunk per section
                  |- OpenAI embeddings -> INSERT into pgvector table
                      |- purge rows whose indexed_at < this run's timestamp
```

Details worth preserving:

- **Repo:** `mmihaylov94/my-portfolio`, branch `main`, folder `knowledgebase/`, `*.md` only.
  Currently **11 files** (`about-mihail`, `contact`, `faq`, `hiring`, `services`, `tech-stack`,
  and five under `projects/`). Small corpus — retrieval quality matters more than scale.
- **Frontmatter keys:** `doc_id`, `source_type`, `page_type`, `title`, `url`, `tags`
  (bracket-list syntax), `last_verified`.
- **Derived metadata:** `doc_id` falls back to a slug of `title`; `source_file` is the last URL
  path segment plus `.md`; `indexed_at` is the run timestamp (ISO 8601).
- **Chunk text** is `"{section_title}\n\n{section_content}"`. Docs with no `##` headings become a
  single chunk with `section = "main"`, `section_title = "Main"`.
- **Chunk metadata** adds `section`, `section_title`, `item_index`, `chunk_source = "markdown_section"`.
- **Staleness handling:** delete-by-`doc_id` before insert, then a sweep deleting any
  `source_type = 'knowledgebase'` row older than the current run — this is how deleted
  source files get removed.

Note the knowledge base is authored as **H2 questions** ("What technology does the … use?"),
which is why section-level chunking works as well as it does. Keep that authoring convention.

### 2.2 `Portfolio | AI Chat -> RAG Vector Store`

```
Chat webhook (public, CORS: https://mihaylov.io, https://www.mihaylov.io)
  |- text classifier -> one of { out_of_scope | small_talk | mihail_related }
       |- out_of_scope   -> canned string, no LLM call
       |- small_talk     -> "Rachel" small-talk agent (short replies, <35 words)
       |- mihail_related -> RAG agent with pgvector retrieval tool (topK 20)
                            + Postgres chat memory (window 25 messages)
```

Details worth preserving:

- **Persona:** "Rachel", the assistant for Mihail Mihaylov. Third person about Mihail, never
  claims to be him, does not introduce herself unless asked.
- **Query rewriting:** the tool description instructs the model to strip filler and the name
  "Mihail" from the search query before retrieving — keyword-style topical queries only.
- **Link allowlist:** never emit URLs containing `/knowledgebase/` or `/projects/`; only a
  fixed set of public links is permitted.
- **Style:** 2–4 sentences, conversational, no headers or bullet lists unless asked, offer more detail.
- **Models:** `gpt-5-mini` for all three LLM nodes; embeddings at 1536 dimensions, batch 512
  (i.e. `text-embedding-3-small`).
- **Tables:** `mihaylov_rag_documents` (vectors), `mihaylov_chat_histories` (memory).

These prompts are long and well tuned. They move into version-controlled prompt files
(§6) essentially verbatim, then get iterated on **with the eval harness measuring the change**.

### 2.3 What the front end does today

Verified against `C:\xampp\htdocs\my-portfolio` on 2026-09-20.

**The portfolio repo is in scope for this work.** Nothing below is a fixed constraint — it is a
snapshot of the starting point, recorded so we know what exists, what can be reused cheaply, and
what is currently claimed but untrue. Where the current setup rules an option out, that is a
cost to weigh, not a wall.

- **Nuxt 4, fully static.** `routeRules: { "/**": { prerender: true } }`; the Docker image serves
  only `.output/public` via nginx. **There is no Nitro server runtime in production.**
- **The chat widget is stock `@n8n/chat`**, mounted by `app/components/AiChatPopup.vue` into
  `#n8n-chat`, posting **directly from the browser** to
  `https://n8n.mihaylov.io/webhook/<NUXT_PUBLIC_N8N_CHAT_WEBHOOK_PATH>`.
- **Session id** lives in `localStorage["n8n-chat/sessionId"]`, with `loadPreviousSession: true`;
  the injected "Start over" button clears it. Not being carried over — see §8.
- **The Express API (`api/src/server.js`) is not involved in chat.** It exposes exactly
  `/api/health` and `/api/contact`. Contact is rate limited (5 per 15 min), reCAPTCHA v3 verified
  (score < 0.5 rejected), then forwarded to an n8n webhook.
- **Deployment** is Docker + Traefik on the external `traefik_proxy` network, images from GHCR
  (`ghcr.io/mmihaylov94/my-portfolio` and `-api`), API routed by `Host(mihaylov.io) && PathPrefix(/api)`.

#### Three things this told us

1. **There is no feedback capture.** `README.md` claims "chat widget … with feedback (thumbs
   up/down)" and the knowledge base article `projects/portfolio-ai-assistant.md` claims
   "Thumbs-up and thumbs-down feedback capture on every answer". Neither is true — the
   portfolio's own `CLAUDE.md` already flags the README as out of date. Deliverable 4 builds
   this for the first time; it is not a port, so budget for it accordingly.
2. **The chat endpoint is currently unauthenticated and unprotected.** The KB article also
   claims "Google reCAPTCHA to protect the endpoint from abuse" — reCAPTCHA guards the *contact
   form* only. The chat webhook is public today, so anyone can spend the OpenAI budget by
   curling it. Fixing this is part of the migration regardless of which design we pick.
3. **The Express API is the right home for the proxy.** The site is fully prerendered with no
   server runtime, so a Nitro route would mean moving to hybrid rendering. Since the Express
   container already exists, already sits at `mihaylov.io/api` behind Traefik, and already does
   this exact pattern for the contact form, chat and feedback become two more routes there. See
   §8 for the alternatives that were weighed.

**Content fix needed:** the assistant currently tells visitors, from its own knowledge base, that
it has feedback capture and reCAPTCHA protection. Both claims are false. That article needs
correcting either way — and it is a neat illustration of the drift deliverable 4 exists to catch.

---

## 3. Decisions

| Area | Decision | Why |
|------|----------|-----|
| Language / runtime | Python 3.12+ | Stated learning goal |
| RAG stack | **OpenAI Python SDK + psycopg 3 + pgvector**, no LangChain | Transparent; every step visible; no abstraction to fight when tuning retrieval |
| API framework | **FastAPI** + Uvicorn | Async, typed, OpenAPI for free |
| Validation / settings | **Pydantic v2** + `pydantic-settings` | Typed config from env, fail fast on boot |
| DB access | **psycopg 3 async** with a connection pool; raw SQL | SQL is part of what is being learned here; an ORM hides the vector query |
| Migrations | **Alembic** against a dedicated `portfolio_rag` schema | Old n8n tables untouched, so both can run in parallel during cutover |
| Evals | **Custom harness + LLM-as-judge** | Control over metrics; swapping models is the whole point |
| Analytics | **SQL views + a CLI digest**, no dashboard | Right-sized for the traffic; see §10 |
| Scheduling | **Dedicated `ingest` container** running supercronic | Isolated from the API, own logs, `docker compose run` for manual runs |
| Browser → API path | **Existing Express API proxies to FastAPI** | Smallest diff; reuses working rate-limit and reCAPTCHA code; no change to how the site renders or deploys; keeps FastAPI off the public internet |
| Chat UI | **New Vue component, redesigned from scratch** | `@n8n/chat` cannot do feedback, citations or a custom endpoint. No obligation to reproduce its UX |
| Session identity | **New scheme; existing conversations are not migrated** | A clean break was accepted, so the id is ours to design rather than inherited from the widget |
| Packaging | **uv** + `pyproject.toml`, `src/` layout | Fast, reproducible lockfile |
| Lint / format | **ruff** (lint + format), **mypy** strict | |
| Tests | **pytest** + `pytest-asyncio`; integration tests run the real migrations into a throwaway schema | Vector SQL cannot be meaningfully mocked |
| Response delivery | **SSE streaming** from day one | A few seconds per answer is long enough that a spinner feels broken; retrofitting means a second pass across FastAPI, Express and Vue |
| Index scope | **`knowledgebase/**/*.md` only** | Already supersedes the site copy; duplicates would compete for retrieval on an 11-document corpus |
| Digest delivery | **Gmail SMTP** (`smtplib`, app password) | No new vendor or account; recipient is env config |
| Build / deploy | **GitHub Actions → GHCR**, server pulls | Mirrors how the portfolio already ships; gives CI a place to gate on ruff/mypy/pytest |
| Repo visibility | **Public** | The system is itself a portfolio artifact. Imposes the hygiene rules in §12 |

### Rejected alternatives

- **LangChain / LangGraph** — closest to the n8n mental model, but the abstraction hides exactly
  the mechanics worth learning, and its pgvector store dictates the schema.
- **Ragas** for evals — good standard metrics, but pulls LangChain back in and makes custom
  metrics (link-rule violations, persona adherence) awkward.
- **APScheduler in the API process** — one fewer container, but couples ingestion load to
  request serving and breaks if the API is ever scaled beyond one replica.
- **A Nuxt Nitro proxy route** — would consolidate everything browser-facing into one repo and
  one language, but costs a move to hybrid rendering, an nginx-to-Node swap in the Dockerfile, a
  new Traefik target and a rewrite of the rate limiting, all to replace code that already works.
- **Retiring Express and exposing FastAPI publicly** — the most Python and the fewest services,
  and tempting given the learning goal. Rejected because it makes the LLM endpoint publicly
  callable, downgrading protection from "holds a secret key" to reCAPTCHA plus rate limiting,
  and because it drags contact-form migration into this project's scope.
- **Public FastAPI with minted session tokens** — recovers most of that security, but is the
  most code of any option (token mint, expiry, refresh, on both sides) for a threat model that
  amounts to one portfolio site's OpenAI bill.
- **A live analytics dashboard (Grafana/Metabase)** — premature at this volume; a weekly digest
  carries the same information at a fraction of the effort.

---

## 4. System overview

```
                     +------------------------------+
  GitHub repo ------>|  ingest container            |
  (knowledgebase/)   |  supercronic -> ingest CLI   |---+
                     +------------------------------+   |
                                                        |  embeddings
  Browser                                               |  (OpenAI)
    |  POST /api/chat                                   |
    v                                                   |
  +---------------------+     +---------------------+   |
  | Express API         |---->| api container       |---+-----------> OpenAI API
  | rate limit,reCAPTCHA|     | FastAPI /v1/chat    |   |  chat
  | holds the bearer key|<----| (no Traefik labels) |   |
  +---------------------+     +---------------------+   |
   mihaylov.io/api             private to the           v
   (already exists)            Docker network
                     +----------------------------------------------+
                     | Postgres 16 + pgvector (schema portfolio_rag) |
                     | documents - chunks - chat_* - eval_* - feedback|
                     +----------------------------------------------+
                                     ^
                     +---------------+--------------+
                     |  eval CLI + analytics CLI    |
                     +------------------------------+
```

The browser never holds the API key. The Express API already sits at `mihaylov.io/api` behind
Traefik and already holds server-side secrets for contact; chat and feedback become two more
routes there, forwarding to FastAPI with the bearer header attached.

---

## 5. Data model

Schema `portfolio_rag`, extension `vector` enabled once at the database level.

### Knowledge base

```sql
documents (
  id            bigserial primary key,
  doc_id        text not null unique,        -- from frontmatter, stable across runs
  source_type   text not null default 'knowledgebase',
  page_type     text,
  title         text not null,
  url           text,
  tags          text[] not null default '{}',
  last_verified date,
  source_file   text,
  source_path   text not null,               -- path within the git repo
  git_blob_sha  text,                        -- from the tree API; skip re-embedding if unchanged
  content_hash  text not null,               -- sha256 of the body
  indexed_at    timestamptz not null,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
)

chunks (
  id              bigserial primary key,
  document_id     bigint not null references documents(id) on delete cascade,
  chunk_index     int not null,              -- item_index in the n8n version
  section         text,                      -- slug
  section_title   text,
  content         text not null,             -- "{section_title}\n\n{body}"
  token_count     int,
  embedding       vector(1536) not null,
  embedding_model text not null,
  created_at      timestamptz not null default now(),
  unique (document_id, chunk_index)
)

create index on chunks using hnsw (embedding vector_cosine_ops);
create index on chunks (document_id);
```

`git_blob_sha` and `content_hash` enable an optimisation n8n did not have: **skip embedding
documents whose content has not changed since the last run.** Only `indexed_at` is bumped.
`--force` re-embeds everything.

### Chat

```sql
chat_sessions  (id, session_id unique, created_at, last_seen_at, client_ip_hash, user_agent,
                referrer, first_seen_path)
chat_messages  (id, session_id fk, role, content, tool_calls jsonb, retrieved_chunk_ids bigint[],
                classification, top_score real, fallback_used boolean,
                model, prompt_tokens, completion_tokens, cost_usd, latency_ms, created_at)
```

Memory is the last N (default 25) messages for a `session_id`, mirroring the n8n context window.
`session_id` is an opaque string minted by the browser (`crypto.randomUUID()` in `localStorage`).
Existing `@n8n/chat` conversations are **not** migrated — a clean break was accepted, so the old
`mihaylov_chat_histories` table is simply dropped at cutover.

The three extra columns exist purely for §10: `top_score` is the best cosine similarity from
retrieval (the content-gap signal), `fallback_used` flags the "I do not have that information"
answer, and `classification` lets us spot mis-routed questions.

### Feedback (new — deliverable 4)

```sql
message_feedback (
  id          bigserial primary key,
  message_id  bigint not null references chat_messages(id) on delete cascade,
  rating      smallint not null check (rating in (-1, 1)),
  comment     text,                          -- optional free text, capped length
  created_at  timestamptz not null default now(),
  unique (message_id)                        -- one vote per answer; re-voting updates
)
```

### Content gaps (derived, refreshed by the analytics job)

```sql
content_gaps (
  id              bigserial primary key,
  cluster_label   text not null,             -- human-readable topic, LLM-named
  centroid        vector(1536) not null,
  question_count  int not null,
  example_questions text[] not null,
  avg_top_score   real,
  negative_rate   real,
  status          text not null default 'open',  -- open | written | wont_fix
  resolved_doc_id text,                      -- the doc written to close it
  first_seen_at   timestamptz not null,
  last_seen_at    timestamptz not null
)
```

This is the table that turns analytics into action: each row is "people keep asking about X and
we answer it badly", and closing it means writing a knowledge base article.

### Evals

```sql
eval_datasets (id, name unique, description, created_at)
eval_cases    (id, dataset_id fk, question, category,          -- mihail_related|small_talk|out_of_scope
               expected_doc_ids text[], reference_answer,
               must_include text[], must_not_include text[],
               source_message_id bigint null,                  -- promoted from real traffic
               notes)
eval_runs     (id, dataset_id fk, label, config jsonb,         -- model, embed model, top_k, prompt version, git sha
               status, started_at, finished_at, totals jsonb)
eval_results  (id, run_id fk, case_id fk, answer, classification,
               retrieved_chunk_ids bigint[], retrieved_doc_ids text[],
               recall_at_k, precision_at_k, mrr,
               judge_scores jsonb, judge_rationale text,
               rule_violations jsonb,                          -- forbidden links, persona breaks
               prompt_tokens, completion_tokens, cost_usd, latency_ms)
```

`eval_cases.source_message_id` is the join that closes the loop: a real question that retrieved
badly becomes a permanent regression test once the missing content is written.

---

## 6. Repository layout

```
ai_assistant/
├── CLAUDE.md                     # context for Claude Code
├── ARCHITECTURE.md               # this file
├── README.md                     # quickstart for a human
├── pyproject.toml / uv.lock
├── .env.example
├── docker/
│   ├── Dockerfile                # multi-stage; one image, several entrypoints
│   ├── compose.yaml              # optional: run the image locally to verify it before deploy
│   ├── compose.prod.yaml         # EC2 deploy template (external Postgres, traefik_proxy network)
│   └── crontab                   # supercronic schedule: ingest daily, analytics weekly
├── alembic.ini                   # minimal; the database URL comes from Settings, not here
├── migrations/                   # alembic
│   ├── env.py                    # URL rewriting, schema creation, version_table_schema
│   └── versions/                 # 0001 knowledge base, 0002 chat+analytics, 0003 evals
├── src/portfolio_ai/
│   ├── config.py                 # Settings (pydantic-settings), single source of env truth
│   ├── logging.py                # structlog JSON logging
│   ├── exceptions.py             # PortfolioAIError root + the errors we raise on purpose
│   ├── db/
│   │   ├── pool.py               # async psycopg pool lifecycle
│   │   ├── documents.py          # upsert doc, replace chunks, purge stale
│   │   ├── chat.py               # session + message persistence, history window
│   │   ├── feedback.py           # upsert a rating for a message
│   │   ├── analytics.py          # the reporting queries
│   │   └── evals.py
│   ├── llm/
│   │   ├── client.py             # AsyncOpenAI wrapper: retries, timeouts, token accounting
│   │   ├── embeddings.py         # batched embedding with backoff
│   │   └── pricing.py            # per-model cost table for reporting
│   ├── ingestion/
│   │   ├── github.py             # tree listing + raw file fetch
│   │   ├── frontmatter.py        # parse + normalise metadata
│   │   ├── chunking.py           # H2 section splitter (+ oversize-section fallback)
│   │   ├── pipeline.py           # orchestration: discover -> diff -> embed -> upsert -> purge
│   │   └── cli.py                # python -m portfolio_ai.ingestion (--dry-run, --force, --only)
│   ├── assistant/
│   │   ├── schemas.py            # ChatRequest / ChatResponse / Citation / FeedbackRequest
│   │   ├── classifier.py         # 3-way intent classification
│   │   ├── retrieval.py          # embed query -> cosine search -> dedupe -> context block
│   │   ├── agent.py              # tool-calling loop over search_knowledgebase
│   │   ├── memory.py             # load/append conversation history
│   │   └── prompts/
│   │       ├── classifier.md
│   │       ├── rag_agent.md      # the "Rachel" system prompt
│   │       ├── small_talk.md
│   │       └── cluster_namer.md  # names question clusters for the digest
│   ├── api/
│   │   ├── main.py               # app factory, CORS, lifespan, exception handlers
│   │   ├── security.py           # bearer auth, rate limiting
│   │   ├── deps.py
│   │   └── routers/
│   │       ├── chat.py           # POST /v1/chat, POST /v1/chat/stream
│   │       ├── feedback.py       # POST /v1/messages/{id}/feedback
│   │       └── health.py         # GET /healthz, /readyz
│   ├── analytics/
│   │   ├── signals.py            # gap detection, rephrase detection, dead-content scan
│   │   ├── clustering.py         # embed + cluster questions, name the clusters
│   │   ├── digest.py             # build the weekly report
│   │   ├── mail.py               # smtplib + Gmail app password; falls back to disk on failure
│   │   ├── retention.py          # the 90-day purge
│   │   └── cli.py                # report | digest | refresh-gaps | gaps | promote-case | purge
│   └── evals/
│       ├── datasets.py           # load/seed golden sets from YAML
│       ├── runner.py             # run a dataset against a config, in parallel
│       ├── judge.py              # LLM-as-judge rubric
│       ├── metrics.py            # recall@k, precision@k, MRR, rule checks
│       ├── report.py             # terminal table + markdown diff between runs
│       └── cli.py
├── datasets/
│   └── golden_v1.yaml            # version-controlled eval cases
└── tests/
    ├── unit/
    └── integration/              # real Postgres, throwaway schema
```

**Prompts live in `.md` files, not string literals** — they are long, they are the main tuning
surface, and diffs on them should be readable in review. Each has a version identifier recorded
in `eval_runs.config` so a score can always be traced back to a prompt.

---

## 7. Ingestion pipeline

```
discover()   GitHub tree API -> [(path, blob_sha)] filtered to knowledgebase/**/*.md
fetch()      raw.githubusercontent.com, concurrent with a semaphore
parse()      frontmatter -> DocumentMeta; body -> markdown
diff()       compare content_hash/git_blob_sha to DB -> changed | unchanged | deleted
chunk()      split on H2; fall back to token-bounded splitting if a section is too large
embed()      batch changed chunks through text-embedding-3-small
persist()    per doc, in one transaction: upsert document, delete old chunks, insert new
purge()      delete documents (cascade chunks) not seen in this run
report()     counts + timings + estimated cost, logged as JSON
```

### Source scope

**`knowledgebase/**/*.md` is the only indexed source.** Settled, not provisional.

The case study pages and the site composables (`useAbout.ts`, `useProjects.ts`) are deliberately
excluded. `about-mihail.md` already carries the experience timeline, tech stack, education,
certifications and contact details those files hold, and every case study has a matching
`projects/*.md` article. Indexing them as well would add near-duplicate chunks competing for the
same slots in a corpus of eleven documents, and would need a second extraction path for Vue and
TypeScript.

The consequence to hold onto: **if the assistant cannot answer something, the fix is to write or
extend a knowledge base article** — never to widen the crawl. That is exactly the loop §10
automates, and keeping one curated source is what makes it work.

### Properties

- **Idempotent.** Re-running with no repo changes is a no-op beyond `indexed_at` bumps.
- **Transactional per document.** A mid-run failure leaves earlier documents correctly updated
  and never leaves a document with zero chunks.
- **Purge is guarded, proportionally.** A run may delete at most `INGESTION_MAX_PURGE_FRACTION`
  of the stored documents — with an `INGESTION_PURGE_GRACE` floor so a three-article corpus is
  not frozen by its own arithmetic — and aborts rather than wiping the knowledge base. The n8n
  version has no guard at all. A fixed minimum was the first design and was rejected: it stops
  protecting as the corpus grows, since a floor of 8 is a real guard over 11 documents and none
  at 50, where a collapse to 9 would clear 41 and still pass.
- **Dry run** prints the plan without touching the DB or spending on embeddings.
- **`doc_id` collisions are rejected, not merged.** Two files claiming one `doc_id` would
  otherwise both be embedded while one silently overwrote the other's chunks — reported as two
  documents indexed and one stored, with no error and one article absent from the index.
- **A document that fails to parse keeps its last good version.** The file still exists upstream,
  so it counts as seen and is not purged; its `indexed_at` is deliberately not advanced, and the
  run exits non-zero. One typo in one frontmatter block should not remove an article from the
  assistant's knowledge.

### Validation at the source

`portfolio-ai-validate` is a command in this package that runs the same `parse_document` and
`chunk_document` against local files — no database, no network, no configuration. The portfolio
repository calls it from the published image on every commit touching `knowledgebase/**`, so a
malformed article fails in review rather than silently dropping out of the index days later.

Keeping the rules in one place is the point: a separate checker in the other repository would
drift, and one that is more lenient than the real parser gives false confidence. Conventions the
validator *cannot* check — headings phrased as questions, sections that stand alone — live in
that repository's `.claude/skills/knowledgebase-articles/` skill.

Schedule: daily at **04:00 Europe/London** (configurable via `docker/crontab`).

---

## 8. Assistant API

### `POST /v1/chat`

```jsonc
// request
{ "message": "Does Mihail work with Laravel?", "session_id": "uuid-from-localStorage" }

// response
{
  "reply": "Yes — Laravel is one of the frameworks Mihail works with most...",
  "message_id": 4812,                  // needed so the browser can attach feedback
  "session_id": "uuid",
  "classification": "mihail_related",
  "citations": [ { "doc_id": "skills-backend", "title": "Backend Skills", "url": null, "section": "php-laravel" } ],
  "usage": { "prompt_tokens": 2841, "completion_tokens": 96, "model": "gpt-5-mini", "latency_ms": 1840 }
}
```

`POST /v1/chat/stream` returns the same content as SSE token deltas for a typing effect — an
upgrade over the n8n webhook, which could only return the finished answer. The `message_id`
is emitted as a final SSE event so feedback still works in streaming mode.

### `POST /v1/messages/{message_id}/feedback`

```jsonc
{ "rating": -1, "comment": "That's not what I asked", "session_id": "uuid" }
```

Requires the `session_id` to match the one that owns the message, so a caller cannot vote on
someone else's conversation. Re-voting updates the existing row rather than inserting.

### Request flow

1. Auth, CORS, rate limit.
2. Load the last 25 messages for `session_id`.
3. Classify the message: `out_of_scope` | `small_talk` | `mihail_related`.
4. Branch:
   - `out_of_scope` — canned reply, **zero further LLM calls**.
   - `small_talk` — small-talk prompt, no retrieval.
   - `mihail_related` — agent loop with the `search_knowledgebase` tool.
5. Persist user and assistant messages with usage metrics, `top_score` and `fallback_used`.
6. Post-process: strip any URL containing `/knowledgebase/` or `/projects/` as a hard code-level
   guard, not just a prompt instruction.

### Retrieval

```sql
select c.id, c.content, c.section_title, d.doc_id, d.title, d.url,
       1 - (c.embedding <=> %(q)s) as score
from portfolio_rag.chunks c
join portfolio_rag.documents d on d.id = c.document_id
where d.source_type = %(source_type)s
order by c.embedding <=> %(q)s
limit %(top_k)s;
```

`top_k` defaults to 20 (n8n parity) but is a config knob the evals tune. With only 11 source
documents, 20 chunks is a large fraction of the whole corpus — one of the first things the evals
should test is whether a smaller `top_k` improves precision and cuts token cost.

### Security

Whatever the front end ends up looking like, these hold:

- `Authorization: Bearer <PORTFOLIO_AI_API_KEY>`, compared with `secrets.compare_digest`.
- **The key never ships in the browser bundle.** It lives in a server-side proxy that adds the
  header; the browser only ever calls a same-origin route. This closes the current hole where
  the n8n webhook is publicly callable by anyone.
- FastAPI is **not** exposed through Traefik — reachable only on the Docker network from the
  proxy. One fewer public surface.
- Rate limits, set deliberately generous for real conversation and backstopped by a hard ceiling:

  | Limit | Value | Enforced in |
  |---|---|---|
  | Per session | 20 messages / 15 min | FastAPI |
  | Per IP | 60 messages / 15 min | Express (`express-rate-limit`) |
  | Global daily spend | configurable USD ceiling | FastAPI |

  The per-request limits stop casual abuse. **The daily cap is what actually protects the bill**,
  because it bounds the worst case however the other two are worked around. On breach it returns
  a polite "back tomorrow" message rather than an error, so a visitor never sees a stack trace.
- Max message length, and a max turns per session, to keep any one conversation bounded.
- reCAPTCHA v3 is already wired up for contact and can gate chat session creation if abuse
  appears.

#### The proxy: existing Express API

Settled. `api/src/server.js` gains two routes mirroring the contact handler it already has:

```
POST /api/chat           -> FastAPI POST /v1/chat
POST /api/chat/feedback  -> FastAPI POST /v1/messages/{id}/feedback
```

It attaches `Authorization: Bearer ${PORTFOLIO_AI_API_KEY}` server-side and forwards to
`${PORTFOLIO_AI_URL}`. Nothing about the Nuxt build, the nginx image or the Traefik routing
changes — the browser is already calling `mihaylov.io/api` for contact, so chat joins the same
origin. Expect roughly 40 lines plus a rate limiter.

Three things this buys:

- **FastAPI is never public.** No Traefik labels; it is reachable only by service name on the
  Docker network. Scanners cannot reach the LLM endpoint at all.
- **The rate limiting already exists.** `express-rate-limit` is configured and working; chat
  gets its own limiter alongside the contact one (which is 5 per 15 minutes — chat needs
  something considerably more generous).
- **reCAPTCHA v3 is already wired up** and can gate chat if abuse appears, without new
  infrastructure.

Streaming is the one wrinkle: proxying SSE through Express needs the response piped rather than
buffered, and compression disabled on that route. Straightforward, but easy to get subtly wrong
— worth an explicit test that tokens arrive incrementally rather than in one chunk at the end.

### Front-end work this requires (portfolio repo)

`@n8n/chat` is dropped entirely and replaced with a purpose-built Vue component. **The UX is a
clean-sheet redesign** — there is no requirement to reproduce the widget's welcome screen,
"Start over" button or conversation behaviour, and **existing conversations are not migrated**,
so the session id scheme is ours to design. A plain `crypto.randomUUID()` in `localStorage` is
enough; the API only needs an opaque stable string.

This also deletes two pieces of accumulated awkwardness: the DOM-querying coupling in
`useAiChat.ts` (which drives the widget by clicking `#n8n-chat .chat-window-toggle`) and the
MutationObserver that injects the "Start over" button and rewrites the heading element. Both are
tied to library internals no type checker guards. `useAiChat().openChat()` becomes a normal
piece of component state, which matters because `useProjects.ts` has project entries with
`opensChat: true` that depend on it.

What the new component has to support, beyond what the widget did:

- **Thumbs up/down per answer**, posting to the feedback route — the point of deliverable 4
- **Citations**, since the API now returns which documents an answer came from
- **Token streaming** via SSE, with the `message_id` arriving as a final event so feedback still
  attaches in streaming mode
- **A visible retention notice**, per the privacy decision in §10

Worth keeping: the CSS custom properties in `AiChatPopup.vue` already encode the site palette
for light and dark mode. Even with a full redesign, those tokens are a free starting point.

---

## 9. Eval harness

**Question it answers:** if the assistant switches to model X, does it actually get better —
and what does it cost?

### Dataset

`datasets/golden_v1.yaml`, version controlled, roughly 40–60 cases spanning:

- `mihail_related` questions with known-correct `expected_doc_ids` (retrieval ground truth)
  and a `reference_answer` (quality ground truth)
- `small_talk` cases
- `out_of_scope` cases (classification must route away)
- Adversarial cases: prompt injection, "are you Mihail?", questions whose answer is genuinely
  absent (the correct answer is the "I do not have that information" fallback), and questions
  whose source content contains a `/knowledgebase/` URL (the link rule must hold)

Seeded into `eval_cases` from YAML. Real questions get promoted into the dataset via
`analytics promote-case` (§10), which is how the set grows beyond what was imagined up front.

### Metrics

**Retrieval** (deterministic, cheap, no LLM):
- `recall@k` — did the expected documents appear in the top k?
- `precision@k`, `MRR` — how well ranked?

**Classification:** accuracy against the labelled category.

**Answer quality** (LLM-as-judge, a strong model scoring 1–5 with a written rationale):
- *Faithfulness* — grounded in retrieved context, nothing invented
- *Completeness* — versus the reference answer
- *Style / persona* — concise, third person, conversational, not a CV dump

**Rule checks** (deterministic): forbidden-link regex, refusal-when-unknown behaviour,
first-person-as-Mihail detection, length bounds.

**Operational:** p50/p95 latency, tokens, `cost_usd` per answer.

### Usage

```bash
uv run eval run --dataset golden_v1 --model gpt-5-mini --label baseline
uv run eval run --dataset golden_v1 --model gpt-5      --label gpt5-test
uv run eval compare baseline gpt5-test        # side-by-side table + per-case regressions
```

Every run stores its full config (chat model, embedding model, `top_k`, prompt versions, git
SHA) so results stay comparable months later. The judge model is pinned independently of the
model under test, and the judge is **never** the same call that produced the answer.

---

## 10. Analytics & the content improvement loop

**Question it answers:** what are people actually asking, where does the assistant fail them,
and what should be written next?

Most of the substrate already exists — `chat_messages` records the question, the retrieved
chunks, the model and the cost. Deliverable 4 adds a feedback signal, a handful of derived
signals, and a report.

### Signals, in rough order of usefulness

Explicit feedback is the *weakest* signal here: few visitors click, and those who do skew
negative. The automatic signals carry far more information.

| Signal | How it is computed | What it tells you |
|---|---|---|
| **Low top score** | `chat_messages.top_score` below a threshold | The question found nothing close. **The primary content-gap signal.** |
| **Fallback rate** | `fallback_used = true` | Questions the docs should answer and do not — a ready-made writing list |
| **Rephrase-and-retry** | Two questions in one session with cosine similarity above a threshold, within a short window | Implicit "that answer was bad". Far more plentiful than thumbs-down |
| **Question clusters** | Embed all questions, cluster, name each cluster with an LLM | The ranked list of what people actually care about |
| **Dead content** | Chunks never in any `retrieved_chunk_ids` | Content nobody reaches — wrong topic, or badly titled |
| **Classification errors** | In-scope questions routed `out_of_scope` | Lost conversations; a classifier prompt bug |
| **Explicit feedback** | `message_feedback` | Low volume, but unambiguous when present |
| **Cost/latency** | Aggregates over `chat_messages` | Budget and UX health |

Question clustering reuses the existing embedding pipeline, so the marginal cost is close to
zero. Cluster naming is one cheap LLM call per cluster per week.

### The loop

```
visitor asks something
  -> low top score / fallback / rephrase / thumbs-down
      -> clustered into a content_gaps row ("people keep asking about X")
          -> write or extend a knowledgebase/*.md article
              -> daily ingest picks it up
                  -> analytics promote-case turns the original question into an eval case
                      -> future model/prompt changes are measured against it
```

The last step is what makes this more than a dashboard. A real failure becomes a permanent
regression test, so the assistant cannot silently get worse at something it was fixed for.

### Interface

```bash
uv run analytics report --since 7d           # the weekly digest
uv run analytics refresh-gaps                # recluster, update content_gaps
uv run analytics gaps --status open          # the writing queue, ranked
uv run analytics promote-case <message_id> --expected-doc project-threadline
```

The digest is a Markdown summary: volume, top clusters, unanswered questions verbatim, worst
retrievals, dead content, feedback tallies, cost. Run weekly by the same supercronic container
as ingestion, and **emailed via Gmail SMTP** using `smtplib` from the standard library — no
third-party mail vendor, no SDK dependency.

Gmail specifics worth knowing up front: it needs an **app password**, not the account password
(ordinary password auth was withdrawn), which in turn requires 2FA on the account. Host
`smtp.gmail.com`, port 587, STARTTLS. The daily send limit is in the hundreds, which is
irrelevant at four emails a month. The recipient is `DIGEST_TO_EMAIL` in the environment —
**never hardcoded**, since this repository is public.

If a send fails the digest is still written to disk and the failure logged, so a broken app
password never silently loses a week of analysis.

### Deliberately not building

**No live dashboard.** With 11 source documents and portfolio-scale traffic, a weekly digest
carries the same information for a fraction of the effort. Revisit if volume grows enough that
a week is too coarse.

### Privacy

This stores real visitors' typed input, and on a portfolio site people volunteer things like
"I'm hiring for a Laravel role at Acme". That needs deciding **before** go-live, not retrofitting:

- **Retention: 90 days** for raw `chat_messages` and their feedback rows, enforced by a daily
  purge in the worker container. Derived data — `content_gaps`, aggregate metrics, and any
  question promoted into `eval_cases` — is kept indefinitely, so the learning survives the
  deletion of the conversation it came from. Long enough to build eval sets and see seasonal
  patterns; short enough to state plainly in a privacy policy.
- IP addresses stored hashed with a salt, never raw. No cookies beyond the existing
  `localStorage` session id.
- A line in the site's privacy policy, and a short note in the chat UI that conversations are
  retained to improve the assistant.
- The digest is for the site owner only; it is not published.

---

## 11. Running it

### Two environments

| | **Local** | **Production** |
|---|---|---|
| Host | Lenovo server on the LAN | EC2 instance |
| Postgres | Docker container, **port 5433** (pgvector) | Existing instance, reached by Docker service name |
| Also there | port 5432 — plain Postgres, **no pgvector** | the portfolio site, n8n and its live databases |
| Contains | nothing live; brand new | `mihaylov_rag_documents`, `mihaylov_chat_histories` |
| App runs | on the dev machine via `uv` | in containers on the EC2 box |

Isolation is at the machine level — two separate boxes, no shared database — so no schema or
database-name juggling is needed. `DATABASE_URL` alone distinguishes them.

Because the local instance holds nothing live, the dev loop is unconstrained: purge, drop and
re-ingest freely. That is a meaningful luxury, and the reason not to develop against EC2.

### Local development

The app runs **on the dev machine** via uv, against the LAN database:

```bash
uv run uvicorn portfolio_ai.api.main:app --reload      # API, sub-second reloads
uv run python -m portfolio_ai.ingestion --dry-run      # ingestion, no writes, no spend
```

```
DATABASE_URL=postgresql://<user>:<pw>@<dev-db-host>:5433/<db>
```

The real host lives in `.env` and nowhere else — see the hygiene rules in §12.

**Port 5433, not 5432.** Both are Postgres; only 5433 has pgvector. Pointing at 5432 gets you a
perfectly healthy connection that fails at the first migration with `type "vector" does not
exist` — a confusing error that reads like a broken migration rather than a wrong port.

Running on the host rather than in Compose means instant reloads, a debugger that just works,
and tracebacks in the terminal — worth a lot while learning the language. Docker locally is only
for verifying the production image before a deploy, not for day-to-day work.

The instance is brand new, so first-time setup is: create the database, then

```sql
CREATE EXTENSION IF NOT EXISTS vector;    -- once per database, needs superuser
```

then `uv run alembic upgrade head`, which creates the `portfolio_rag` schema and everything in
it. If the extension is missing the first migration fails loudly rather than half-applying.

One consequence of the database being across the network rather than on localhost: a batch of
embedding inserts pays LAN round-trips. Use `execute_many`/`COPY` for chunk inserts rather than
a loop of single statements — worth doing anyway, but noticeable here in a way it would not be
on a local socket.

**Tests never touch the `portfolio_rag` schema.** Integration tests create a uniquely named
`pytest_*` schema on the same instance, run the real migrations into it, and drop it afterwards.
A session-scoped fixture does this once per run, and orphans from a crashed run are swept at the
start of the next one. The fixture refuses to run unless `ENVIRONMENT` is `local`.

Unit tests need nothing external and are the default suite; integration tests are opt-in with
`-m integration`, so the whole suite does not require the database to be reachable.

Testcontainers was the original plan and was dropped: Docker is not installed on the development
machine. CI pins a pgvector service container instead, which is where version parity actually
matters.

### Production (EC2)

One image, several entrypoints, joining the existing `traefik_proxy` network so the Express API
can reach it by service name.

```yaml
networks:
  traefik_proxy:
    external: true

services:
  api:
    image: ghcr.io/mmihaylov94/portfolio-ai:latest
    container_name: portfolio-ai
    command: uvicorn portfolio_ai.api.main:app --host 0.0.0.0 --port 8000
    env_file: .env
    networks: [traefik_proxy]
    restart: unless-stopped
    # deliberately no traefik labels — internal only, reached at http://portfolio-ai:8000

  worker:
    image: ghcr.io/mmihaylov94/portfolio-ai:latest
    command: supercronic /etc/crontab
    env_file: .env
    networks: [traefik_proxy]
    restart: unless-stopped
```

`docker/crontab`:

```cron
0 4 * * *   python -m portfolio_ai.ingestion          # daily, 04:00 Europe/London
30 5 * * 1  python -m portfolio_ai.analytics digest   # weekly, Monday
0 3 * * *   python -m portfolio_ai.analytics purge    # retention sweep
```

Set `TZ=Europe/London` on the worker so those times mean what they say.

- **Postgres is on `traefik_proxy` and reached by service name** — no published ports, no
  credentials crossing a host boundary. It is the existing instance on the EC2 box, the one n8n
  uses; this project only adds the `vector` extension and its own `portfolio_rag` schema, and
  leaves the live n8n tables alone until cutover.
- The Express API container gains `PORTFOLIO_AI_URL=http://portfolio-ai:8000` and
  `PORTFOLIO_AI_API_KEY` in its `.env`, plus the `/api/chat` and `/api/chat/feedback` routes.
  It is already on `traefik_proxy`, so it resolves the FastAPI service by name.
- Manual ingestion: `docker compose run --rm worker python -m portfolio_ai.ingestion --force`.
- Migrations as a one-shot on deploy: `docker compose run --rm api alembic upgrade head`.
- Logs are JSON to stdout, picked up by the existing Docker logging setup.

### CI/CD

GitHub Actions, mirroring how the portfolio already ships:

```
push to main
  -> ruff check + ruff format --check
  -> mypy
  -> pytest (unit; integration via a pgvector service container)
  -> docker build + push ghcr.io/mmihaylov94/portfolio-ai:latest
  -> server pulls
```

The test and type gates run **before** the image is built, so a red build never produces a
deployable tag. This folder is not a git repository yet — `git init` and creating the GitHub repo
are part of step 1.

### Configuration

All via environment, validated by `Settings` at startup — the process refuses to boot on a bad
config rather than failing on the first request.

| Variable | Notes |
|---|---|
| `OPENAI_API_KEY` | one key for embeddings, chat and judge |
| `DATABASE_URL` | `postgresql://...` |
| `DB_SCHEMA` | default `portfolio_rag` |
| `PORTFOLIO_AI_API_KEY` | bearer token; also set in the Express API's `.env` |
| `GITHUB_REPO` / `GITHUB_BRANCH` / `GITHUB_DOCS_PATH` | `mmihaylov94/my-portfolio` / `main` / `knowledgebase` |
| `GITHUB_TOKEN` | optional; raises the API rate limit, required if the repo goes private |
| `CHAT_MODEL` / `CLASSIFIER_MODEL` / `EMBEDDING_MODEL` / `JUDGE_MODEL` | defaults `gpt-5-mini`, `gpt-5-mini`, `text-embedding-3-small`, `gpt-5` |
| `EMBEDDING_DIMENSIONS` | `1536` — changing this requires a re-embed and a migration |
| `RETRIEVAL_TOP_K` | default `20` |
| `MEMORY_WINDOW` | default `25` |
| `LOW_SCORE_THRESHOLD` | cosine score below which a question counts as a content gap |
| `RATE_LIMIT_SESSION` / `RATE_LIMIT_IP` | `20/15m` / `60/15m` (the IP limit is enforced in Express) |
| `DAILY_SPEND_CAP_USD` | hard ceiling; on breach the API returns a polite refusal, not an error |
| `CHAT_RETENTION_DAYS` | default `90`; `0` disables the purge job |
| `IP_HASH_SALT` | salt for hashing client IPs |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_APP_PASSWORD` | `smtp.gmail.com` / `587` / the Gmail address / **app password, not the account password** |
| `DIGEST_TO_EMAIL` | digest recipient — env only, never committed |
| `TZ` | `Europe/London`, so the crontab times mean what they say |
| `CORS_ORIGINS` | comma-separated |
| `LOG_LEVEL` / `ENVIRONMENT` | |

`.env.example` ships with placeholder values for every one of these and real values for none —
this repository is public.

---

## 12. Build order

1. **Foundations** — `git init` and create the public GitHub repo, `pyproject.toml`, settings,
   logging, DB pool, Alembic and the initial migration against the **existing local pgvector
   container**, pytest fixtures, lint/type/test commands, and the Actions workflow. Set
   the hygiene rules up front (§12) rather than scrubbing history later.
2. **Ingestion** — GitHub client, frontmatter parser, chunker, embeddings, pipeline, CLI.
   Verify: row counts and chunk boundaries match what n8n produced.
3. **Assistant core** — retrieval, classifier, agent loop, memory, prompts ported from n8n.
   Verify: spot-check answers against the live n8n bot.
4. **API** — FastAPI, auth, rate limiting, SSE streaming, health checks, **plus the feedback
   endpoint and the analytics columns**. Capture is cheap to build now and impossible to
   backfill later.
5. **Evals** — dataset, runner, metrics, judge, reporting. Establish the `gpt-5-mini` baseline.
6. **Front end + cutover** — add `/api/chat` and `/api/chat/feedback` to the Express API, build
   the redesigned Vue chat component (streaming, citations, thumbs up/down, retention notice),
   remove `@n8n/chat` and its DOM coupling, then deactivate the n8n workflows and drop
   `mihaylov_rag_documents` and `mihaylov_chat_histories`. This is the largest chunk of
   portfolio-repo work in the project and is best treated as its own piece of planning.
7. **Analytics reporting** — signals, clustering, digest, `promote-case`. Deliberately last:
   analytics on zero traffic tells you nothing, and the queries will be better designed after
   seeing a few hundred real questions.

Step 5 sits before cutover because the baseline is what proves parity. Step 7 sits after it
because it needs traffic — but step 4 must already be recording the data it will read.

Also in step 6, independent of the code: **fix `knowledgebase/projects/portfolio-ai-assistant.md`**,
which currently describes an n8n-based system with feedback capture and reCAPTCHA-protected chat.
After cutover almost none of that article is true. Since this repository is public, the rewritten
article can link to it — which is a decent part of the reason for making it public.

### Public-repo hygiene

Non-negotiable from the first commit, because git history is published too:

- `.env` gitignored; `.env.example` carries placeholder values only.
- **No personal email addresses** anywhere in code or docs. The digest recipient is
  `DIGEST_TO_EMAIL`; the addresses in `knowledgebase/contact.md` are already public by choice and
  are not this repository's concern.
- **No IPs or hostnames in committed files** — not the LAN address of the dev server, not the
  EC2 host, not container names. They belong in `.env` only. A private-range address is low risk
  by itself, but it maps out your network for anyone reading, and it accretes: one IP in a README
  becomes three in a runbook. `compose.prod.yaml` is a template, not a copy of the live file, and
  the only host appearing anywhere in the repo is `mihaylov.io`.
- Prompts, schema and eval datasets are all fine to publish — none of them are secrets, and they
  are much of what makes the repo worth reading.
- Real chat logs never leave the database. The `datasets/` directory holds curated eval cases
  only, and a question promoted from real traffic gets read before it is committed.

---

## 13. Decisions log

**All open questions are closed.** Requirements are final; implementation can start at step 1.

| Question | Decision | Where |
|---|---|---|
| RAG stack | OpenAI SDK + psycopg 3 + pgvector; no LangChain | §3 |
| Eval approach | Custom harness + LLM-as-judge | §9 |
| Scheduling | Dedicated worker container running supercronic | §11 |
| Database | Fresh `portfolio_rag` schema, Alembic migrations | §5 |
| Index scope | `knowledgebase/**/*.md` only — gaps become new articles | §7 |
| Proxy location | Existing Express API; FastAPI stays private | §8 |
| Chat UI | Clean-sheet redesign, reusing the existing palette tokens | §8 |
| Session continuity | Not preserved; `crypto.randomUUID()` in `localStorage` | §8 |
| Streaming | SSE from day one | §8 |
| Rate limits | 20/session/15m, 60/IP/15m, plus a daily spend ceiling | §8 |
| Retention | 90 days raw; derived data kept indefinitely | §10 |
| Digest delivery | Gmail SMTP via `smtplib`, recipient in env | §10 |
| Ingestion schedule | Daily, 04:00 Europe/London | §11 |
| Environments | Two: local (LAN server, dev-only) and production (EC2) | §11 |
| Postgres access (prod) | Existing EC2 instance, same Docker network, by service name | §11 |
| Local database | Existing pgvector container on the LAN server, **port 5433** | §11 |
| Local dev loop | App on the dev machine via `uv run`; Docker only to verify the image | §11 |
| Test database | Throwaway `pytest_*` schema on the dev instance; CI pins a service container | §11 |
| Build / deploy | GitHub Actions → GHCR, server pulls | §11 |
| Repo visibility | Public, with the hygiene rules in §12 | §12 |

### Confirm at deploy time

Not blocking, and no code depends on them:

- The exact `DAILY_SPEND_CAP_USD` figure — pick one once there is a week of real cost data.
- The `LOW_SCORE_THRESHOLD` cutoff — needs real retrieval scores to calibrate; start at a guess
  and tune from the first digest.
- Whether the site's privacy policy needs new wording or just a sentence added.
