# Lessons

This project is built as a learning exercise as much as a working system. Each lesson lands a
real piece of it, explains every Python-specific decision in the code, and ends with one exercise
done against the real codebase. Nothing here is a toy.

The lessons assume you can already program — they explain what is *Python-specific*, not what a
function or a test is. Where it helps, they compare against PHP and Node, since that is the
background they were written for.

Read them in order. Later ones assume the earlier ones.

## Step 1 — Foundations

| # | Lesson | What it lands | Done |
|---|---|---|---|
| 1 | [Tooling and uv](01-tooling-and-uv.md) | `pyproject.toml`, `uv.lock`, `.gitignore` | ☑ |
| 2 | [Packages, modules and imports](02-packages-modules-imports.md) | package skeleton, `exceptions.py`, `py.typed` | ☑ |
| 3 | [Typed configuration](03-typed-configuration.md) | `config.py`, `.env.example` | ☑ |
| 4 | [Structured logging](04-structured-logging.md) | `logging.py` | ☑ |
| 5 | [Async Python](05-async-python.md) | `cli.py`, `concurrency.py` | ☑ |
| 6 | [Postgres with psycopg 3](06-postgres-with-psycopg.md) | `db/pool.py` | ☑ |
| 7 | Migrations with Alembic | `migrations/`, the full schema | ☐ |
| 8 | Testing | `tests/`, testcontainers | ☐ |
| 9 | Types and linting | strict mypy, ruff rules | ☐ |
| 10 | Docker and CI | `Dockerfile`, GitHub Actions | ☐ |

Later steps (ingestion, the assistant, the API, evals, analytics) get their own lessons once the
foundation is in place. The build order is in [ARCHITECTURE.md](../../ARCHITECTURE.md) §12.

## How each lesson is laid out

1. **What you'll understand** — the takeaways
2. **Why it matters here** — how it connects to this project specifically
3. **The concepts** — the actual teaching
4. **The code** — a walkthrough of what was written and why
5. **Coming from PHP / Node** — where your existing instincts help, and where they mislead
6. **Exercise** — one task, 15–30 minutes, against this codebase
7. **Check yourself** — questions, answers at the bottom

Do the exercises. Reading about a lockfile and regenerating one are different kinds of knowing.
