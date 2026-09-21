# Lesson 6 — Postgres with psycopg 3

## What you'll understand

- Why database code is pooled, async, and wrapped in `async with` — all for the same reason
- Why parameterised queries make injection *impossible* rather than merely difficult
- Why a table name can't be a parameter, and what to do instead
- That psycopg opens a transaction the moment you touch the database, and what that costs if you forget
- Three real bugs this lesson hit, none of which reported the actual problem

## Why it matters here

This is the first lesson where the code leaves the process. Everything so far could be checked by
reading it. From here there's a second program involved, with its own rules, its own state, and
its own ideas about what you meant.

Every stored document, every embedding, every chat message and every eval result goes through the
file this lesson builds. And unlike the last five lessons, a mistake here can affect something
outside your program.

## The concepts

Here's the sentence the rest of this hangs on.

> Postgres is a separate program. Everything you do is send it a message down a connection and
> wait for the reply.

Not a library you call. Not a file you open. **Another running program**, usually on another
machine, that you talk to over a network socket. Once that's in your head the rest stops looking
like arbitrary ceremony.

### Why the database layer is async

You're waiting on another program. That's precisely the situation lesson 5 was about — the CPU
sits idle while a reply comes back over the network, and async is how that idle time gets used
for something else.

A blocking database driver inside an async application is the trap from lesson 5 in its most
expensive form: every query freezes the entire process. That's why this project uses psycopg's
async interface rather than its ordinary one.

### Why connections are pooled

A connection isn't a handle to a file. It's a TCP socket, an authentication handshake, and a
process allocated on the server to serve you. Opening one takes real time, and the server caps
how many can exist at once **across everything using it** — so on a shared instance, your
connections are a share of something other applications are also drawing on.

So you don't open one per query. You open a few, keep them, and hand them out as needed. That's
the pool.

`settings.db_pool_max_size` is that share. It's a setting rather than a constant because the
right number differs between a laptop running one script and a server running an API.

### Why everything is `async with`

Borrowing something you must give back is exactly what a context manager is for:

```python
async with pool.connection() as conn, conn.cursor() as cur:
    await cur.execute("select 1 as ok")
    row = await cur.fetchone()
```

At the end of the block the connection goes back to the pool — **including if the body raised**.
That's the whole point. Without it, one exception on an unlucky path leaks a connection, and
you'd discover it days later when the pool ran dry.

The comma form borrows both on one line. Nesting two `async with` blocks means the same thing and
costs an indent.

A **cursor** is the thing that runs a statement and holds the results. It isn't a list — it's a
position in a result set on the server, which is what lets you pull a million rows without
loading a million rows into memory.

### Why parameterised queries are safe

This is the important one, and it's usually taught wrongly.

```python
await cur.execute("select * from docs where title = %s", (title,))
```

The common explanation is that the library "escapes" the value — puts backslashes in the
dangerous bits. That's not what happens, and the truth is better.

**The query text and the values travel as separate fields in the protocol.** The server receives
the statement, works out its structure, and *then* receives the values to slot into the gaps. By
the time your value arrives, the server has already decided that position holds a value. There's
no parsing step left for it to escape into.

Injection isn't hard here. It's *impossible*, because the value never passes through a parser.

Two things to note about `%s`: it isn't Python's `%` formatting, despite looking identical — it's
psycopg's placeholder marker, and psycopg never lets those two meet. And it's always `%s`
regardless of type: strings, integers, dates, lists all use `%s`.

### Why a table name can't be a parameter

Parameters are values. A table or schema name is part of the *structure* — it's what the server
uses to work out what the statement means, before any values arrive. So this cannot work:

```python
await cur.execute("select * from %s", ("documents",))   # no
```

Which is a genuine problem for us, because the schema name comes from settings, and putting it in
the query text is exactly what we just said never to do.

`psycopg.sql.Identifier` is the answer:

```python
sql.SQL("set search_path to {}").format(sql.Identifier(settings.db_schema))
```

It quotes and escapes as an identifier. Even a hostile value becomes harmless:

```
Identifier("portfolio_rag")       ->  "portfolio_rag"
Identifier('x"; drop table y; --')->  "x""; drop table y; --"
```

The second is a valid, absurd, entirely inert table name. Nothing executes.

So the rule has two halves worth keeping straight: **`%s` for values, `sql.Identifier` for names,
f-strings for neither.**

### Transactions start whether you asked or not

psycopg 3 opens a transaction the moment you execute anything, and it stays open until something
commits or rolls back. Leaving the `async with pool.connection()` block commits it — or rolls it
back if the body raised.

That default is a good one. It means a block of related writes either all happen or none do,
without you having to say so. But it has a sharp edge, which the third bug below is entirely
about.

## The code

`db/pool.py` is four functions: create the pool, close it, set up each connection, and one query
you can use to check it all worked.

The interesting part isn't the code — it's that **writing it hit three separate bugs, and not one
of them reported the actual problem.** They're worth more than the file is.

### Bug one: the wrong event loop

The very first attempt to connect:

```
InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
```

Windows defaults to an event loop implementation that psycopg's async mode can't work with. The
fix is one line, and it has to run before any loop starts:

```python
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
```

It's in `portfolio_ai/__init__.py`, which means it runs when the package is imported — **directly
contradicting** the rule from lessons 3 and 4 about modules not doing work on import.

That's deliberate, and worth being honest about rather than quietly hoping you don't notice. The
rule is a good one. But the alternative here is two lines of boilerplate in every one-liner, every
test file and every script that touches the database, plus a baffling error whenever it's
forgotten. On Linux the line does nothing, so production never runs it.

Rules worth keeping are worth breaking visibly when the cost of keeping them is higher than the
cost of the exception. Writing down why is what stops it becoming folklore.

### Bug two: a schema path that hid the extension

Each connection gets `search_path` set, so queries can say `from chunks` rather than
`from portfolio_rag.chunks`. The obvious version:

```python
set search_path to "portfolio_rag"
```

Every connection then died during setup, and the pool reported a timeout after thirty seconds.
The actual error, once dug out:

```
ProgrammingError: vector type not found in the database
```

The pgvector extension is installed into `public`. Setting the search path to only our schema
removed `public` from it, so the `vector` type became invisible — and registering it is the next
thing the setup function does.

The fix is to keep `public` on the path, second:

```python
set search_path to "portfolio_rag", "public"
```

Our tables win any name collision because our schema comes first; extension types stay reachable.
That's why a search path is a *list* rather than a single value, which is easy to read past until
it costs you half an hour.

### Bug three: a transaction left open

With that fixed, the pool still timed out. The real error was buried in the pool's own logging:

```
connection left in status INTRANS by configure function: discarded
```

`INTRANS` means "inside a transaction". The setup function runs two statements, and per the
section above, psycopg opened a transaction at the first one. The pool requires connections
handed back from setup to be idle — so it discarded that one, opened another, discarded that too,
and eventually gave up and reported a timeout.

One line fixes it:

```python
await conn.commit()
```

Note what all three have in common. **Not one reported the actual problem.** A timeout that means
a missing extension. A timeout that means an open transaction. An error naming an internal class
that means "you're on Windows". Reading the error carefully was necessary in all three cases and
sufficient in none — the thing that actually worked was reducing the failure to the smallest
program that still showed it.

### A note on rows

The pool sets `row_factory=dict_row`, so a row comes back as `{"title": ..., "secret": ...}`
rather than a tuple. Tuples are positional, so `row[0]` silently changes meaning the day someone
reorders a `SELECT`. Column names don't.

## Coming from PHP / Node

| | Python (psycopg 3) | PHP (PDO) | Node (pg) |
|---|---|---|---|
| Placeholder | `%s` | `?` or `:name` | `$1` |
| Values passed | second argument | `execute([...])` | second argument |
| Pool | `AsyncConnectionPool` | usually none | `pg.Pool` |
| Transactions | implicit, on first statement | explicit `beginTransaction()` | explicit `BEGIN` |

**You already know the safety model.** `$stmt = $pdo->prepare("... WHERE id = ?")` followed by
`$stmt->execute([$id])` is the same mechanism for the same reason — query and values sent
separately. Laravel's query builder does it for you, which is why `DB::raw()` carries the warnings
it does. If prepared statements make sense to you in PHP, `%s` needs no further explanation.

**Pooling is the part that's new, and it comes from the process model.** In PHP a request gets a
process, opens a connection, and the process ends — there's nothing to pool, and nothing to give
back. Here one long-lived process handles many things at once, so connections are a resource with
a lifecycle you own. `pg.Pool` in Node is the direct equivalent.

**The transaction default is the one that will surprise you.** PDO doesn't start a transaction
until you call `beginTransaction()`. psycopg starts one at your first statement, whether or not
you wanted it. That's convenient — related writes are atomic without ceremony — and it's exactly
what bug three was.

## Exercise

Two parts. The second is the one to make time for.

### Part 1 — write the health check

`health_check()` in `pool.py` currently returns `False`. It should ask the database the simplest
question there is and report whether it answered.

`current_search_path()` directly above it is the pattern — borrow a connection, get a cursor,
execute, fetch. Your query is `select 1`, and give the column a name so the dictionary row has a
sensible key.

One real decision: **this must not raise.** It'll be the API's readiness probe, and a readiness
check that throws is no use to whatever is checking readiness. So you need to catch something —
and *what* to catch is the interesting part. Catch `Exception` and you'll swallow genuine bugs and
report a healthy database that isn't. Catch nothing and it's not a health check. Look at what
psycopg actually raises when a database is unreachable, and pick deliberately.

Test it by running it, then by stopping the database container and running it again.

### Part 2 — try to inject

Save this as `injection_demo.py` in the project root:

```python
import asyncio

from portfolio_ai.db.pool import close_pool, get_pool

SETUP = """
create temporary table docs (id serial primary key, title text, secret text);
insert into docs (title, secret) values
  ('public-page', 'harmless'),
  ('internal-notes', 'CONFIDENTIAL'),
  ('drafts', 'CONFIDENTIAL');
"""


async def main():
    pool = await get_pool()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(SETUP)

        evil = "x' OR '1'='1"

        await cur.execute("select title, secret from docs where title = %s", (evil,))
        print("parameterised ->", await cur.fetchall())

        await cur.execute(f"select title, secret from docs where title = '{evil}'")
        print("f-string      ->", await cur.fetchall())

        worse = "x'; drop table docs; select 'gone"
        await cur.execute(f"select title from docs where title = '{worse}'")
        await cur.execute("select to_regclass('pg_temp.docs') is null as gone")
        print("table dropped ->", await cur.fetchone())

    await close_pool()


asyncio.run(main())
```

It uses a temporary table, which exists only for that connection and disappears afterwards, so
nothing in your database is at risk.

**Predict all three lines before running it.** Then run it, and answer:

1. The parameterised query returns nothing at all. Why is that the *correct* result rather than a
   failure?
2. The f-string version returns three rows including both `CONFIDENTIAL` secrets. Walk through
   what the database actually received.
3. The third one drops the table. Given `%s` and an f-string look almost identical in the source,
   what makes one safe and the other not? Answer in terms of what crosses the wire.

Delete the file afterwards and check `git status` is clean.

## Check yourself

1. Why is a connection pooled when a file handle isn't?
2. Why can `%s` hold a document title but not a table name?
3. What does `async with pool.connection()` guarantee that a plain function call wouldn't?
4. You set `search_path` to just your schema and the next query can't find a type. Why?
5. Why did an unclosed transaction in the setup function show up as a pool *timeout*?
6. Why does the project use `dict_row` rather than the default tuples?

---

<details>
<summary>Answers</summary>

1. Because it's a socket to another program, not a handle to local data. Opening one costs a
   network handshake, authentication, and a process on the server — and the server limits how many
   can exist at once, across every application sharing it. Files are cheap and local; connections
   are expensive and shared.

2. Because parameters are values, and the server works out the *structure* of a statement before
   the values arrive. A table name is part of that structure, so it has to be in the query text —
   which is why `sql.Identifier` exists, to put it there safely.

3. That the connection goes back to the pool when the block ends, including if the body raised.
   Without it, one exception on an unlucky path leaks a connection permanently, and you find out
   when the pool runs dry.

4. Because `search_path` is a list, and replacing it dropped `public` — where extensions like
   pgvector install their types. Our schema needs to come first so our tables win collisions, but
   `public` has to stay on the list so extension types remain reachable.

5. Because the pool refuses a connection returned from its setup function in a transaction, and
   discards it. It then tries again, discards that one too, and keeps going until the caller's
   wait expires. The timeout is the symptom of every attempt failing; the cause never appears in
   it.

6. Because tuples are positional. `row[0]` means whatever happens to be first in the `SELECT`, so
   reordering columns silently changes what your code reads. Column names survive that.

</details>
