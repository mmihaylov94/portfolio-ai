# Portfolio AI

A retrieval-augmented assistant for the knowledge base behind [mihaylov.io](https://mihaylov.io),
built in Python to replace an existing n8n implementation.

Four parts:

1. **Ingestion** — reads Markdown from a GitHub repository, chunks it by section, embeds it with
   OpenAI and upserts into Postgres with pgvector. Runs daily.
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
- **[docs/lessons/](docs/lessons/)** — the project built as a lesson series, one concept at a time
- **[CLAUDE.md](CLAUDE.md)** — working context for Claude Code

## Status

Under construction. Step 1 (foundations) is in progress; the build order is in
[ARCHITECTURE.md](ARCHITECTURE.md) §12.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync                                          # create the environment from the lockfile
uv run python -c "import portfolio_ai; print(portfolio_ai.__version__)"
```

Copy `.env.example` to `.env` and fill it in once lesson 3 lands. A Postgres instance with the
`pgvector` extension is needed from lesson 7 onward.

## Licence

[MIT](LICENSE)
