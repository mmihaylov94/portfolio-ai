# Lesson 7 — Migrations with Alembic

## What you'll understand

- Why a database needs a *history* rather than just a schema
- What the version table is, and why every confusing migration problem traces back to it
- Why our migrations are written by hand rather than generated
- What happens when the version table and the database disagree — demonstrated, not described
- Why downgrades are a development convenience rather than a rollback plan

## Why it matters here

This is where ARCHITECTURE.md §5 stops being a document and becomes tables. After this lesson the
`search_path` from lesson 6 points at a schema that actually exists, and everything from step 2
onward has somewhere to write.

It's also the first exercise that **changes** the database rather than reading from it. Your local
instance holds nothing yet, so there's nothing to lose — but it's worth noticing the shift.

## The concepts

You could create your schema by running a `.sql` file. That works exactly once, on one machine.

The moment there's a second environment the problem changes shape. Your laptop has the schema from
last week. The server has the schema from last month. A colleague has one nobody can account for.
"Run the CREATE TABLE" doesn't help, because the answer depends on what's already there — and
nobody can tell you what's already there.

So the schema needs a *history*: an ordered list of changes, and a record of how far along each
database has got.

> The database remembers which migrations it has already run. Everything else follows from that
> one stored value.

### The version table

Alembic creates a table called `alembic_version`. One column, one row, one value — the identifier
of the last migration applied:

```
version   : 0003
```

That's it. That's the entire state Alembic keeps. Everything it does is: read that value, look at
the chain of migrations, run the ones that come after it, write the new value.

Almost every confusing thing you'll ever hit with migrations is this value disagreeing with what's
actually in the database. You'll see exactly that happen later in this lesson.

### The chain

Each migration names its parent:

```python
# 0002_chat_and_analytics.py
revision      = "0002"
down_revision = "0001"      # the one before me
```

That makes them a linked list, not just files in alphabetical order. `alembic history` walks it:

```
0002 -> 0003 (head), Eval datasets, cases, runs and results
0001 -> 0002, Chat history, feedback, and the content gaps derived from them
<base> -> 0001, Knowledge base: documents, chunks, and the vector index
```

`<base>` is the empty database, `head` is the newest. `alembic upgrade head` means "walk from
wherever this database is to the end". A fresh database runs all three; one already at `0002` runs
only the last.

### Two halves to each step

Every migration has an `upgrade()` and a `downgrade()` — the two directions along the chain.

```bash
alembic upgrade head      # forwards, to the newest
alembic downgrade -1      # back one step
alembic current           # where is this database?
alembic history           # what is the chain?
```

**Being honest about downgrades:** ours are fully written, and they're genuinely useful *locally* —
step back, change something, step forward. In production they're a different matter. Dropping a
column discards its data, and no downgrade brings that back. What production does instead is roll
*forward*: write a new migration that undoes the problem, so the history keeps moving in one
direction and nothing is silently lost.

So: real downgrades, because they make development pleasant and the exercise possible. Not a
rollback plan.

### Why ours are hand-written

Alembic can generate migrations for you with `--autogenerate`. It works by comparing ORM model
definitions against the live database and writing the difference.

We have no models. ARCHITECTURE.md §3 chose raw SQL over an ORM, so there's nothing to diff, and
autogenerate has nothing to work from. That's a consequence of an earlier decision rather than
something missing.

It's not much of a loss. Our migrations are the SQL from the design document, which means you can
read a migration next to §5 and check they agree.

### A migration that has run is frozen

Once a migration has been applied anywhere you can't reach, it's immutable. Edit it and the
version table still says it ran — but the database no longer matches what the file says it did.

Need a change? Write a new migration. The history is append-only for the same reason git history
is.

## The code

### `alembic.ini`

Nearly empty, deliberately. In particular it does **not** contain the database URL.

The stock template puts `sqlalchemy.url` here. That would mean two places knowing how to reach the
database — `Settings` and this file — and the one in version control would be the wrong one, or
worse, a real credential in a public repository.

### `migrations/env.py`

This is where the work is. Three things it does that the stock template doesn't, each because of a
problem this project actually hit.

**It rewrites the URL.** SQLAlchemy reads `postgresql://` as "use psycopg2" — the older driver,
which we don't install:

```
postgresql://...          -> ModuleNotFoundError: No module named 'psycopg2'
postgresql+psycopg://...  -> OK, driver = psycopg
```

Alembic runs on SQLAlchemy even though nothing else here does, so `env.py` names the driver
explicitly on the way through. `DATABASE_URL` stays in its ordinary form everywhere else, because
psycopg reads it directly and has no such ambiguity.

**It creates the schema.** Alembic puts its version table in `portfolio_rag`, and creates it
*before* running any migration. So the schema has to exist first — and no migration can create it,
because the version table comes first. A chicken-and-egg that has to be solved here:

```python
connection.execute(CreateSchema(settings.db_schema, if_not_exists=True))
```

**It sets the search path**, same as the pool in lesson 6, so migrations write `create table
documents` without repeating the schema name on every statement.

**And it configures logging.** With the ini file's logging section deleted, `alembic upgrade`
printed *absolutely nothing* — which is unnerving for a deploy step, where silence and success
look identical. Calling `configure_logging()` routes Alembic's own reporting through the chain
from lesson 4:

```json
{"event": "Running downgrade 0003 -> 0002, Eval datasets...", "logger": "alembic.runtime.migration", "level": "info"}
```

Alembic has never heard of structlog. It logs through Python's built-in logging, which lesson 4
wired up — so it arrives as JSON with everything else, for free.

**One thing it deliberately doesn't do:** Alembic can run "offline", printing SQL instead of
executing it, for handing to a DBA. We deploy by running migrations directly, so that mode isn't
wired up.

### The migrations

Three, split by deliverable: knowledge base, chat and analytics, evals. Splitting them isn't
cosmetic — a chain of one has nothing to demonstrate, and `downgrade -1` needs somewhere to step
back *to*.

They use `op.execute()` with plain SQL rather than Alembic's typed operations, for the same reason
there's no ORM: the SQL *is* the design. You can read `0001_knowledge_base.py` beside §5 and check
them against each other. The downgrades use `op.drop_table()`, which is concise and where a typed
operation genuinely reads better.

A few decisions worth pulling out, all documented in the files:

**The extension goes in `public`.** Extensions are database-wide, and putting `vector` in our
schema would mean every connection needed our schema on its search path just to know what a vector
is. It's also why lesson 6 kept `public` second on the path.

**`on delete cascade` from chunks to documents**, because a chunk has no meaning without its
document. But `on delete set null` from `eval_cases.source_message_id` to `chat_messages` — the
90-day retention sweep will eventually delete the original message, and the eval case must outlive
it. Cascade there would quietly delete your regression tests.

**`cost_usd` is `numeric`, not a float.** Money in binary floating point accumulates error, and
this column gets summed.

**`client_ip_hash`, not `client_ip`.** The privacy decision from §10, encoded in the column name so
storing a raw address looks wrong.

**The HNSW index specifies `vector_cosine_ops`.** That has to match the distance operator the query
uses — §8 orders by `<=>`, which is cosine. An index built for a different operator isn't wrong, it's
*ignored*, and you get a sequential scan with no error to tell you.

### The bug this lesson hit

Writing this, `0003`'s downgrade was `pass` — it's your exercise. Then:

```
Running downgrade 0003 -> 0002, Eval datasets, cases, runs and results
```

Reported success. Alembic wrote `0002` into the version table and moved on. But the downgrade did
nothing, so the tables were still there:

```
eval tables present : 4
version table says  : 0002
```

The record and the reality now disagree. The next `upgrade head` tried to create tables that
already existed:

```
psycopg.errors.DuplicateTable: relation "eval_datasets" already exists
```

**Alembic never checks.** It has no idea what your `upgrade()` or `downgrade()` actually did — it
runs the function and, if nothing raised, updates the version. A downgrade that quietly does
nothing is indistinguishable from one that worked.

Which is the practical argument for the round trip in your exercise. Writing a downgrade takes a
minute; the only way to know it's correct is to run it.

The repair, for when this happens to you:

```bash
alembic stamp 0003     # "the database really is at 0003" -- record without running anything
```

`stamp` writes the version table without executing migrations. It's the tool for exactly this:
reality is fine, the record is wrong, so fix the record.

### The thing that saved it: transactional DDL

Trying `alembic downgrade base` with that same no-op downgrade produced a second, more
interesting failure. Walk it through:

- `0003 -> 0002` does nothing, so the `eval_*` tables survive
- `0002 -> 0001` tries to drop `chat_messages` — and **fails**, because `eval_cases` still has a
  foreign key pointing at it

Two of three steps had already "run". You'd expect a half-dismantled database. Instead:

```
version : 0003 (head)
tables  : all eleven, intact
```

Postgres supports **transactional DDL** — `CREATE TABLE` and `DROP TABLE` can live inside a
transaction and roll back like anything else. Alembic wraps the whole run in one, so a chain of
migrations either completes or leaves nothing behind. That line in its startup output is not
decoration:

```json
{"event": "Will assume transactional DDL.", "logger": "alembic.runtime.migration"}
```

This is worth knowing because **it is not true everywhere**. MySQL commits implicitly on DDL, so a
failure halfway through leaves exactly the mess you'd fear, and migration tooling there has to be
written far more defensively. If you've been taught to dread a failed migration, that's usually
where it comes from.

It also shows why the ordering constraint in your exercise reaches across files. `eval_cases`
references `chat_messages`, so `0003`'s downgrade must remove those tables before `0002`'s
downgrade can do its job. Foreign keys make downgrade order a property of the whole chain, not of
one migration.

## Coming from PHP / Node

| | Alembic | Laravel | Node |
|---|---|---|---|
| A migration | `upgrade()` / `downgrade()` | `up()` / `down()` | varies by tool |
| State | one row in `alembic_version` | rows in `migrations` | varies |
| Order | `down_revision` chain | filename timestamps | usually filenames |
| Generation | `--autogenerate` from models | `make:migration` stub | varies |

**Laravel will feel very close.** `up()`/`down()` are `upgrade()`/`downgrade()`, `migrate` is
`upgrade head`, `migrate:rollback` is `downgrade -1`. If you've used Laravel migrations you already
have the model.

**Two differences worth knowing.**

Laravel tracks migrations as *a row per migration* plus a batch number, so it knows individually
what has run. Alembic stores **one value** — a pointer into a chain. That's why order is explicit
via `down_revision` rather than inferred from filenames, and it's why a wrong value breaks things
more comprehensively: there's only one thing to be wrong.

And `make:migration` gives you a stub to fill in, while `--autogenerate` tries to write the
migration *for* you by diffing your models. Ours are hand-written because there are no models to
diff — closer to the Laravel experience than the Alembic one, as it happens.

## Exercise

Write the `downgrade()` for `0003_evals.py` and prove it works.

### Part 1 — write it

The file has a marked gap. `0001` and `0002` both have their downgrades written, so there's a
pattern to copy.

The part that bites is **order**. Look at the foreign keys in `upgrade()`:

- `eval_results` references both `eval_runs` and `eval_cases`
- `eval_cases` and `eval_runs` both reference `eval_datasets`

Drop a table something still points at and Postgres refuses. The rule is: reverse of creation
order, children before parents.

### Part 2 — prove the round trip

**This is the exercise.** A downgrade that has never been run is a guess — as the section above
demonstrates.

```bash
uv run alembic current                    # should say 0003
uv run alembic downgrade -1               # back to 0002
uv run alembic current                    # should say 0002
```

Then check the database itself:

```bash
uv run python -c "
import asyncio
from portfolio_ai.db.pool import get_pool, close_pool

async def main():
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(\"select tablename from pg_tables where schemaname='portfolio_rag' order by 1\")
        for r in await cur.fetchall():
            print(' ', r['tablename'])
    await close_pool()

asyncio.run(main())
"
```

Two things to confirm, and the second matters as much as the first:

1. The four `eval_*` tables are **gone**
2. `documents`, `chunks`, `chat_*`, `content_gaps` are **still there** — a downgrade that took
   more than its own migration would be a much worse bug than one that took too little

Then `uv run alembic upgrade head` and confirm all eleven tables are back.

**If you get the order wrong**, Postgres will tell you plainly, the migration will fail, and the
version table won't move — which is Alembic's transactional DDL protecting you. Fix and re-run.

**If your downgrade silently does nothing**, you'll get the mess from the section above. `alembic
stamp 0003` puts the record back.

## Check yourself

1. What is actually stored in `alembic_version`, and why is it only one value?
2. Why can't `env.py`'s schema creation be done in a migration instead?
3. Why are our migrations hand-written when Alembic can generate them?
4. A downgrade that does nothing still "succeeds". What does that tell you about what Alembic checks?
5. Why does the HNSW index name `vector_cosine_ops`, and what happens if it's wrong?
6. Why is `eval_cases.source_message_id` `on delete set null` rather than `on delete cascade`?

---

<details>
<summary>Answers</summary>

1. The identifier of the last migration applied — one row, one column. It's one value because
   migrations form a chain, so a single position in that chain is enough to know what has run and
   what hasn't. It's also why a wrong value is so disruptive: there's only one thing to be wrong.

2. Because Alembic creates its version table — inside that schema — *before* running any migration.
   The schema has to exist before the first migration gets a chance to run, so it can't be a
   migration's job.

3. Autogenerate diffs ORM models against the database, and there are no models here: ARCHITECTURE.md
   §3 chose raw SQL. It's little loss — the migrations are the SQL from the design document, so a
   migration and §5 can be read against each other.

4. That Alembic doesn't check anything. It calls your function and, if nothing raises, writes the
   new version. It has no idea what the function did or was supposed to do. Which is why the only
   way to know a downgrade is correct is to run it.

5. Because it has to match the distance operator the query uses — `<=>` is cosine. If they don't
   match, the index isn't wrong, it's *ignored*: you get a sequential scan over every row, correct
   results, and no error saying why it got slow.

6. Because the 90-day retention sweep will eventually delete the original chat message, and the eval
   case has to outlive it. Cascade would silently delete regression tests as old conversations were
   purged — losing exactly the thing the analytics loop worked to produce.

</details>
