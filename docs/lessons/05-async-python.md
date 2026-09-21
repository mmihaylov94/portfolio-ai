# Lesson 5 — Async Python

## What you'll understand

- What `await` actually does, and why it is a pause rather than a second thread
- Why async helps when you're *waiting* and does nothing when you're *working*
- The trap: code that uses `gather`, looks concurrent, and isn't — with no error to tell you
- Why calling an async function doesn't run it, unlike a JavaScript promise
- Why async spreads through a codebase instead of staying in one corner

## Why it matters here

Everything from the next lesson onward is async. Every database query, every OpenAI call, the
whole API. If the model in your head is wrong, the code will still run — it'll just be slow in
ways that produce no error and no clue.

That's the thing worth taking seriously about this topic. A mistake in lesson 3 gave you a
`ConfigError` naming the field. A mistake here gives you a program that works perfectly and takes
ten times longer than it should, and nothing anywhere will mention it.

**Good news first, though:** if you've written modern JavaScript, you already know this model.
Node is single-threaded with an event loop, and `await` there does what `await` does here. Most
of what follows will feel familiar. The section on PHP and Node covers the one difference that
actually catches people, and it's worth reading even if the rest feels obvious.

## The concepts

Here's the whole thing.

> There is only one thread. `await` marks a spot where your function pauses and hands control
> back, so that something else can run while it waits.

That's it. Everything below is a consequence.

### Waiting is not working

Imagine fetching ten files over the network, each taking 0.2 seconds. During those 0.2 seconds
your program isn't doing anything — it's sat waiting for a reply. The CPU is idle.

Async is a way of using that idle time. While one function waits for its reply, another can get
on with sending its request. **There's no extra thread and nothing runs in parallel.** It's one
worker who, instead of standing still waiting for the kettle, goes and puts the bread in the
toaster.

Which tells you exactly when async helps and when it doesn't:

- **Waiting** — network calls, database queries, reading a file. Async helps enormously.
- **Working** — parsing, arithmetic, clustering a few thousand embeddings. Async does nothing at
  all, because there's no idle time to reuse. One worker, one task.

The analytics clustering in deliverable 4 is genuinely CPU work. Async will not make it faster,
and expecting it to is the most common disappointment with the whole approach.

### `await` is a pause, and only a pause

```python
async def fetch_document(url):
    response = await http_get(url)     # pause here; something else may run
    return response.text
```

At the `await`, this function stops and says *I'm going to be a while — go and do something
else*. When the reply comes back, it picks up where it left off.

Crucially, `await` does **not** mean "start this". It means "wait for this". So:

```python
a = await fetch(1)      # wait for the first to finish...
b = await fetch(2)      # ...and only then start the second
```

is just as sequential as ordinary code. Two waits, one after the other. This is the single most
common misunderstanding, and it's why the first timing in your exercise is what it is.

### Starting things together

To actually overlap the waiting, something has to start them all before waiting on any:

```python
results = await asyncio.gather(fetch(1), fetch(2), fetch(3))
```

`gather` sets all three going, then waits for the lot. Now the three waits overlap, and the whole
thing takes about as long as the slowest one rather than all three added together.

### Why you can't just gather everything

`gather` starts *everything*, immediately. Ten items, fine. A thousand items means a thousand
simultaneous connections, which gets you rate-limited by OpenAI, refused by GitHub, or straight
through your database's connection limit.

So you cap it. A `Semaphore` is a counter with a maximum: before starting, a task asks for a slot
and waits if none are free; on finishing, it gives the slot back. Ten tasks with a limit of three
means three run, and as each finishes the next begins.

That's what `gather_limited` in `concurrency.py` does, and it's why ARCHITECTURE.md §7 specifies
that ingestion fetches "concurrent with a semaphore" rather than just concurrently.

### The trap

This is the part to remember.

```python
async def fetch(i):
    time.sleep(0.2)        # WRONG
    return i

await asyncio.gather(*(fetch(i) for i in range(10)))
```

That takes 2.0 seconds. The `gather` bought you nothing whatsoever.

`time.sleep` is a **blocking** call. It doesn't hand control back — it just stops, holding the one
thread the entire program has. Nothing else can run, because there's nothing else to run *on*.
Every other task sits frozen until it returns.

And nothing tells you. No error, no warning. The code has `async`, it has `await`, it has
`gather`, and it runs perfectly at the speed of doing everything one at a time.

The async equivalent, `await asyncio.sleep(0.2)`, does hand control back. One character of
difference in spirit, ten times the difference in speed. You'll measure exactly this in the
exercise.

The same trap applies to any ordinary blocking library. `requests.get()` blocks;
`httpx.AsyncClient.get()` doesn't. Regular file reading blocks. The blocking version is usually
the one you already know, which is what makes this so easy to walk into.

### When you can't avoid blocking

Sometimes there's no async version. This project has a real case: the weekly digest sends email
through `smtplib`, which is part of the standard library and thoroughly blocking.

```python
await asyncio.to_thread(send_the_email, message)
```

`to_thread` runs the blocking function on a separate thread and lets your coroutine wait for it
properly — so the loop keeps serving everything else. It's the designated escape hatch, and it's
what you reach for when a library gives you no async option.

### Calling an async function doesn't run it

Worth its own section because it differs from JavaScript, and the difference is silent.

```python
c = work()      # nothing has happened
await c         # now it runs
```

`work()` builds a **coroutine object** and returns it. The body hasn't executed. Nothing happens
until something awaits it.

In JavaScript, calling an async function starts it immediately and hands you a promise that's
already in flight. In Python you get an object that hasn't begun.

This has a practical consequence you'll see in `cli.py`. If you create a coroutine and then never
await it — because something failed first — Python complains on the way out:

```
RuntimeWarning: coroutine 'main' was never awaited
```

Harmless, but it clutters an error message at exactly the moment you want a clear one.

### Async spreads

A normal function can't `await`. So if a function needs to await something, it must be `async`,
and everything calling it must await *it*, and so on up the stack until you reach the top.

People describe this as async being "contagious", usually as a complaint. It's better understood
as a consequence of the one idea: `await` is a pause, and only a function that's allowed to pause
can contain one.

The practical upshot is that you decide once, for a whole program, rather than case by case.
ARCHITECTURE.md says "async throughout" for this reason. The only sync code is at the very edge,
where `asyncio.run` starts the loop.

## The code

### `concurrency.py`

One function. `gather_limited(limit, *awaitables)` runs them all with at most `limit` in flight:

```python
semaphore = asyncio.Semaphore(limit)

async def guarded(awaitable):
    async with semaphore:
        return await awaitable

return await asyncio.gather(*(guarded(a) for a in awaitables))
```

`async with` is the async version of `with`. It acquires a slot, runs the body, and releases the
slot afterwards — **including if the body raises**, which is the reason to use it rather than
acquiring and releasing by hand. Every database call in the next lesson uses `async with` too.

Two behaviours worth knowing, both documented in the file:

**Results come back in the order you passed them in**, not the order they finished. So you can
line results up against whatever you built them from without tracking which was which.

**If one raises, the others aren't cancelled.** The exception reaches you, but the remaining work
carries on in the background. That's a genuinely sharp edge in `asyncio.gather` itself, and it
matters if you were expecting an early failure to stop everything.

There's also `[T]` after the function name — a generic parameter, saying "whatever type these
awaitables produce is the type of the list coming back". It does nothing at runtime; it lets a
type checker follow the values through. Lesson 9 covers this properly.

### `cli.py`

The wrapper every command-line entry point will use. It configures logging, starts the event
loop, and turns failures into exit codes:

```python
def run_async(main: Callable[[], Coroutine[Any, Any, None]]) -> int:
```

Look at what it takes: **the function itself, not the result of calling it.** `run_async(main)`,
never `run_async(main())`. That's the coroutine-laziness point from above — if we accepted a
coroutine object and then `configure_logging()` failed, we'd discard it unawaited and Python
would print that `RuntimeWarning` on top of the real error. Taking the function means nothing
exists until we're ready to run it.

The exit codes matter because this runs from cron inside a container, where nobody reads the
output unless something looks wrong:

- `0` — fine
- `1` — a `PortfolioAIError`. Something we rejected deliberately, with a message already written
  for a human. Logged, no traceback.
- `130` — Ctrl-C. The convention is 128 plus the signal number, and SIGINT is 2. Interrupting a
  program is a decision, not a malfunction, so a traceback showing where in the event loop it
  landed helps nobody.

Anything else — a `TypeError`, a `KeyError` — isn't caught, and gets its traceback. Those are
bugs, and bugs should be loud. That's the `PortfolioAIError` split from lesson 2 earning its
keep.

## Coming from PHP / Node

| | Python | Node | PHP |
|---|---|---|---|
| Model | one thread, event loop | one thread, event loop | one process per request |
| Keyword | `await` | `await` | — |
| Run everything at once | `asyncio.gather` | `Promise.all` | — |
| Call starts it? | **no** | **yes** | — |
| Default library style | **blocking** | **non-blocking** | blocking |

**Node is the same model**, and your instincts transfer. An event loop, one thread, `await`
yields, `Promise.all` is `gather`. If async has ever felt frightening, it shouldn't — you've been
writing it for years.

**Two differences, and the second one is the one that bites.**

The smaller: in JavaScript, `const p = work()` starts the work immediately and gives you a promise
already running. In Python, `c = work()` gives you a coroutine that hasn't begun. Usually this
doesn't matter, and occasionally it explains something confusing.

The bigger: **Node's ecosystem is non-blocking by default, and Python's is blocking by default.**
In Node you have to go out of your way to block — `fs.readFileSync` has "sync" in the name,
shouting at you. In Python the blocking version is the normal one, with the obvious name, and
it's almost always the one you already know. `time.sleep`, `requests.get`, `open().read()` — all
blocking, all completely ordinary-looking, all capable of freezing your entire program inside an
`async def` without a word of complaint.

That's why this project uses `httpx` rather than `requests`, and psycopg's async interface rather
than its normal one. It's not preference. A single blocking call in the wrong place removes the
benefit of every async decision around it.

**PHP doesn't really have an equivalent** worth stretching for. A request gets a process, the
process blocks, and concurrency is the web server's job. Sharing one thread between many
in-flight operations is genuinely a different world, and mapping it onto Laravel would confuse
more than it clarified.

## Exercise

Measure the four cases and explain the numbers. **The deliverable is your reasoning, not code.**

Save this as `async_timings.py` in the project root:

```python
import asyncio
import time


async def fetch(i):
    await asyncio.sleep(0.2)      # pretend network call
    return i


async def fetch_blocking(i):
    time.sleep(0.2)               # the trap
    return i


async def sequential():
    return [await fetch(i) for i in range(10)]


async def all_at_once():
    return await asyncio.gather(*(fetch(i) for i in range(10)))


async def limited(n):
    sem = asyncio.Semaphore(n)

    async def guarded(i):
        async with sem:
            return await fetch(i)

    return await asyncio.gather(*(guarded(i) for i in range(10)))


async def blocking():
    return await asyncio.gather(*(fetch_blocking(i) for i in range(10)))


async def main():
    for name, coro in [
        ("sequential       ", sequential()),
        ("gather (all 10)  ", all_at_once()),
        ("gather (limit 3) ", limited(3)),
        ("gather + blocking", blocking()),
    ]:
        start = time.perf_counter()
        await coro
        print(f"{name} {time.perf_counter() - start:.2f}s")


asyncio.run(main())
```

Run it with `uv run python async_timings.py`.

**Predict each number before you look.** Write your four guesses down first — that's the entire
point of the exercise. Then run it and answer:

1. **Sequential is about 2.0s.** Why, given every one of those calls is `await`ed?
2. **`gather` is about 0.2s.** What changed? Ten calls still happened.
3. **Limit 3 is about 0.8s.** Why that, and not 0.6s or 1.0s? Work through what the ten tasks are
   actually doing over those 0.8 seconds. This is the one worth spending time on.
4. **The blocking version is about 2.0s.** It uses `gather` exactly as case 2 does. Why did it
   gain nothing — and what would have told you, if you hadn't measured it?

Delete the file afterwards and confirm `git status` is clean.

## Check yourself

1. Why does async speed up ten network calls but not a heavy calculation?
2. `await a()` then `await b()` — is that concurrent? Why not?
3. Why is `time.sleep(0.2)` inside an `async def` worse than pointless?
4. Why does `run_async` take the function rather than the coroutine?
5. If one awaitable in `gather_limited` raises, what happens to the others?
6. Why can't a normal `def` contain an `await`?

---

<details>
<summary>Answers</summary>

1. Because async reuses time that would be spent waiting, and there's only one thread. Network
   calls spend nearly all their time idle, so that time can be given to something else. A
   calculation has no idle time — the thread is busy — so there's nothing to reuse and no second
   thread to move it to.

2. No. `await` means "wait for this to finish", not "start this". The first call completes before
   the second begins, exactly as in ordinary sequential code. Overlapping them needs something
   that starts them all first, like `gather`.

3. Because it holds the only thread without handing control back, so every other task in the
   program is frozen until it returns. Worse than pointless because it's invisible: the code
   looks async, uses `gather`, produces no error, and runs at sequential speed.

4. Because calling an async function creates a coroutine that hasn't started. If we took that
   object and something failed before the loop ran, it'd be discarded unawaited and Python would
   print `RuntimeWarning: coroutine was never awaited` over the top of the actual error. Taking
   the function means nothing is created until it's about to be run.

5. The exception reaches the caller, but the others are **not** cancelled — they carry on running
   in the background. That's `asyncio.gather`'s own behaviour, and it matters if you expected one
   failure to stop the rest of the work.

6. Because `await` is a pause that hands control back to the event loop, and only a function
   declared `async` can be suspended and resumed that way. A normal function runs start to finish
   once called. This is why async propagates up through everything that calls it.

</details>
