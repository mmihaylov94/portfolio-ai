# Portfolio AI

[![CI](https://github.com/mmihaylov94/portfolio-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/mmihaylov94/portfolio-ai/actions/workflows/ci.yml)

A retrieval-augmented assistant for the knowledge base behind [mihaylov.io](https://mihaylov.io),
built in Python to replace an existing n8n implementation.

Four parts:

1. **Ingestion** — reads Markdown from a GitHub repository, chunks it by section, embeds it with
   OpenAI and upserts into Postgres with pgvector. Runs hourly.
2. **Assistant** — a RAG chat API answering questions about that documentation, behind an
   authenticated endpoint.
3. **Evals** — a harness for comparing models and prompts on retrieval quality, answer quality,
   latency and cost.
4. **Analytics** — captures what visitors ask and where the assistant fails them, clusters the
   gaps, and feeds real failures back into the eval set.

Deliberately built without a RAG framework: the OpenAI SDK, psycopg and raw SQL, so every
embedding call, vector query and agent loop iteration is visible in the source.

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — design, data model, decisions and the reasoning behind them
- **[docs/INGESTION.md](docs/INGESTION.md)** — how ingestion works: one run step by step, which module does what, the libraries
- **[docs/ASSISTANT.md](docs/ASSISTANT.md)** — how the assistant works: one question step by step, which module does what, the libraries
- **[docs/API.md](docs/API.md)** — how the HTTP API works: one request step by step, the contract, the limits, the libraries
- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — how it is deployed, and the one-command redeploy
- **[docs/lessons/](docs/lessons/)** — the project built as a lesson series, one concept at a time
- **[CLAUDE.md](CLAUDE.md)** — working context for Claude Code

## Status

Under construction, and useful already.

- **Foundations** — packaging, configuration, logging, the database layer, migrations, tests, the
  lint and type gates, CI and the published image.
- **Ingestion** — deployed, and syncing the knowledge base hourly.
- **Assistant core** — the assistant answers end to end from a terminal: it classifies each
  message, searches the knowledge base, answers from what it finds, remembers the conversation and
  records what every answer cost.
- **API** — the assistant over HTTP, private to the server's Docker network: authenticated,
  rate-limited, capped on daily spend, streaming answers as server-sent events, with thumbs
  up/down feedback and a nightly 90-day retention sweep.

Next are the evals, which establish the baseline the assistant is judged against before the site
switches over. The build order is in [ARCHITECTURE.md](ARCHITECTURE.md) §12.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                                      # create the environment from the lockfile
cp .env.example .env                         # then fill it in

uv run ruff check . && uv run ruff format .  # lint and format
uv run mypy                                  # types, strict
uv run pytest                                # unit tests
uv run pytest -m integration                 # needs a reachable pgvector database

uv run alembic upgrade head                  # create the schema

uv run python -m portfolio_ai.ingestion --dry-run   # plan an ingest; no writes, no spend
uv run python -m portfolio_ai.ingestion             # sync the knowledge base
uv run portfolio-ai-validate path/to/knowledgebase  # check articles; no DB, no network

uv run python -m portfolio_ai.assistant             # chat with the assistant in the terminal
uv run python -m portfolio_ai.assistant -v          # and show what each search found
uv run python -m portfolio_ai.assistant -m "..."    # ask one question and exit

uv run uvicorn portfolio_ai.api.main:app --reload   # the API, reloading on changes; /docs for the schema
uv run python -m portfolio_ai.analytics purge --dry-run   # what the retention sweep would delete
```

A Postgres instance with the `pgvector` extension is required. `DATABASE_URL` must point at it
and not at a plain Postgres on the same host — the settings validator refuses a local URL on the
wrong port, because connecting to the wrong one succeeds and then fails at the first migration
with `type "vector" does not exist`.

## Building the image

One image runs both the API and the scheduled jobs; the command decides which.

```bash
docker build -f docker/Dockerfile -t portfolio-ai .   # context is the repository root
docker compose -f docker/compose.yaml up --build      # run it locally against your own .env
```

CI runs the same lint, type and test commands on every push and pull request, builds the image
either way, and pushes `ghcr.io/mmihaylov94/portfolio-ai` only from `main`.
`docker/docker-compose.yml` is the deploy template.

## Licence

[MIT](LICENSE)
