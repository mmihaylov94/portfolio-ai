# Lesson 4 — Structured logging

## What you'll understand

- Why a log line should be data rather than a sentence
- What a "processor" is, and why the whole of this lesson is one idea repeated
- The three pieces of Python's built-in logging system, and how they fit together
- How output from a library that has never heard of our code ends up in our format
- What `contextvars` gives you that passing arguments around does not

## Why it matters here

Everything from here on produces output worth keeping. Ingestion reports how many documents it
touched and what the embeddings cost. The assistant records which chunks it retrieved and how
long the model took. And the whole of deliverable 4 in `ARCHITECTURE.md` — the analytics that
tell you what people asked and where the assistant failed them — is built on numbers that have
to come from somewhere.

None of that survives as prose in a terminal.

There's also a thread from last lesson. `SecretStr` stops the OpenAI key printing when someone
logs the settings object, but it does nothing about a dictionary from somewhere else that happens
to contain a token — an API response, a database row, a set of request headers. This repository
is public and these logs go to a server, so a second layer belongs here. That's your exercise.

And this is the lesson that finally creates `portfolio_ai/logging.py`, the file lesson 2 promised
would look alarming and turn out to be fine.

## The concepts

Here is the way almost everyone writes a log line for the first ten years of their career:

```python
logger.info(f"Indexed {count} documents in {duration}ms")
```

It produces a sentence, which is fine if a person is going to read it and nothing else ever will.
The trouble starts when you want to answer a question. *How many documents did we index last
week?* You can't add up sentences. You'd have to write something that picks the number back out
of the text it was baked into — and then rewrite it the day someone changes the wording.

The alternative is to stop writing sentences:

```python
log.info("documents_indexed", count=11, duration_ms=840)
```

Same information, different shape. `documents_indexed` is now a name you can count occurrences
of, and `count` is a number sitting in its own field rather than dissolved into a sentence.

That shape is what makes the rest possible, and it rests on a single idea.

> A log call does not print a sentence. It builds a dictionary, and that dictionary is handed
> along a line of small functions before anything reaches the screen. Each one can add to it,
> change it, or throw it away.

Everything below is that idea in a different costume.

### The line of functions

`structlog` calls those functions **processors**, which sounds grander than it is. A processor is
an ordinary function. It takes the dictionary, does something, and gives it back.

Our chain, in `_processor_chain()`, is seven of them in order. Given the call above, the
dictionary grows as it travels:

```
log.info("documents_indexed", count=11)

  starts as         {"event": "documents_indexed", "count": 11}
  merge_contextvars {"event": ..., "count": 11, "run_id": "abc123"}
  add_logger_name   {..., "logger": "portfolio_ai.ingestion"}
  add_log_level     {..., "level": "info"}
  TimeStamper       {..., "timestamp": "2026-09-21T09:12:25Z"}
  format_exc_info   (adds "exception" only if one is being handled)
  _redact_secrets   (your exercise — currently changes nothing)

  then rendered     {"event": "documents_indexed", "count": 11, "run_id": "abc123", ...}
```

Order matters, and only for obvious reasons: the timestamp has to be added before anything can
render it, and redaction has to happen before the dictionary is turned into text — after that
there's no dictionary left to redact.

The last step, rendering, is just another function. It's the one that takes the finished
dictionary and turns it into the line you actually see. Ours produces JSON. Swapping it for one
that produces coloured, aligned, human-friendly output is a one-line change, which is worth
knowing about for later.

### Why JSON, even on your laptop

One object per line, everywhere, including while you're developing.

The honest tradeoff: JSON is harder to read as it scrolls past than a nicely formatted line
would be, and you'll be reading a lot of it over the next six lessons. What you get for that is
that there's no difference between what you see locally and what the server produces — so a log
line can't look fine on your machine and turn out wrong in production, and there's no
environment-dependent branch to reason about.

If it becomes genuinely annoying, `_processor_chain()` and the `JSONRenderer()` call in
`configure_logging()` are where you'd change it.

### Context that follows you around

Suppose an ingestion run touches eleven documents and you want every log line from that run
tagged with the same run ID. The obvious approach is to pass it into every function that might
log — which means changing a dozen signatures to carry a value that most of them don't care
about.

`contextvars` is the way out:

```python
structlog.contextvars.bind_contextvars(run_id="abc123")
```

From that point on, `run_id` appears on every log entry in that context, without anything being
passed anywhere. The `merge_contextvars` processor sits first in our chain precisely to pull
those in.

"Context" here means the current thread — and, importantly, the current async task, which
matters from lesson 5 onwards. Two requests being handled at once each get their own; they don't
tread on each other.

### Python's built-in logging, in three pieces

This bit isn't really about structlog, and you need it to understand the second half of the file.

Python ships with a `logging` module, and every third-party library uses it — psycopg, httpx,
uvicorn, all of them. It has three moving parts:

- A **logger** is the named thing code calls. `logging.getLogger("psycopg.pool")`. Names are
  dotted and form a tree, so `psycopg.pool` sits under `psycopg`, which sits under the **root**
  logger at the top. A message travels *up* that tree.
- A **handler** decides where output goes — a file, the terminal, the network.
- A **formatter** decides what it looks like on the way out.

The useful consequence of the tree is that you don't have to configure each library separately.
Attach one handler to the root logger and everything ends up going through it, because everything
eventually travels up to the root.

### How library output joins our format

Which is exactly what `configure_logging()` does. It attaches a single handler to the root
logger, and gives that handler a formatter which runs the structlog chain.

The result is that a library line and one of ours take the same route and come out the same
shape:

```json
{"count": 11, "event": "documents_indexed", "run_id": "abc123", "logger": "portfolio_ai.ingestion", "level": "info", ...}
{"event": "connection pool exhausted", "run_id": "abc123", "logger": "psycopg.pool", "level": "warning", ...}
```

Look at the second line. That came from a library calling plain `logging.getLogger(...).warning()`
— it has never heard of structlog, and it isn't cooperating with us in any way. It still came out
as JSON, and it still picked up `run_id` from our bound context.

That's the one idea earning its keep. Once everything flows through the same line of functions,
you can do things to *all* of it at once — which includes redacting secrets from libraries that
never thought about secrets.

### Exceptions

`format_exc_info` in the chain turns a live exception into a field:

```json
{"event": "ingest_failed", "level": "error", "exception": "Traceback (most recent call last): ..."}
```

One entry, with the traceback inside it, rather than a stack trace sprayed across the output
interleaved with whatever else was happening. Use `log.exception("...")` inside an `except`
block and this happens automatically.

## The code

### `logging.py`

Three functions. `_redact_secrets` is your exercise and currently does nothing.
`_processor_chain()` returns the list described above. `configure_logging()` wires up both halves.

Two decisions in there worth pointing at.

**It's a function you call, not something that happens on import.** Same reasoning as
`get_settings()` last lesson: importing a package shouldn't quietly reconfigure the entire
process's logging. That belongs to whoever owns the program — an entry point calls
`configure_logging()` once, deliberately.

**`root.handlers.clear()` before adding ours.** Without it, calling `configure_logging()` twice
would attach two handlers, and every line would appear twice. That's a genuinely common bug and
it's confusing when you hit it, because nothing looks wrong — you just get everything in
duplicate.

**Output goes to stdout and stops there.** No files, no rotation, no cleanup. The container
captures stdout, and lesson 10's Docker setup is where that gets picked up. Writing log files
from inside a container is a way of losing them.

### The filename

`portfolio_ai/logging.py` sits next to code that says `import logging` and means the standard
library. Lesson 2 said this would be fine, and here's the proof from the file itself:

```
our module : \logging.py
stdlib     : C:\...\Python312\Lib\logging\__init__.py
```

Both correct. Python only looks at what's lying *directly* inside the folders on its search path,
and our file is one level deeper than that, inside the `portfolio_ai` folder. To reach it you
have to name the package.

## Coming from PHP / Node

| | Python here | Laravel | Node |
|---|---|---|---|
| Library | structlog over stdlib `logging` | Monolog | pino / winston |
| Structured by default | yes | no — `Log::info('text', [...])` | yes, pino |
| Per-request context | `contextvars` | manually, or a middleware | `AsyncLocalStorage` |
| Config | `configure_logging()` | `config/logging.php` | at logger creation |

If you've used pino, this will feel familiar — it's the same model, and for the same reasons.

Monolog is the closer comparison for the structured bit, and worth being precise about. Laravel's
`Log::info('User created', ['id' => $user->id])` does carry a context array, so the data is
there. The difference is that the message stays a sentence and the context is a sideshow, whereas
here the "message" is really just a name and everything else is a field. `Log::info("Indexed {$n}
documents")` is the habit worth dropping in both languages.

The piece with no real equivalent is the root logger trick. In PHP you'd typically configure each
channel; here the fact that logger names form a tree means one handler at the top catches
everything, including libraries you didn't know were logging.

## Exercise

Write the redaction processor — `_redact_secrets` in `logging.py`.

Right now it hands the dictionary straight back, so secrets go into the logs in full. You can see
it happening:

```bash
uv run python -c "
import structlog
from portfolio_ai.logging import configure_logging
configure_logging()
structlog.get_logger('demo').info('calling_api', api_key='sk-this-should-not-be-here')
"
```

### The mechanics, since you need them

A **dictionary** is a set of key-and-value pairs. `event_dict` might be:

```python
{"event": "calling_api", "api_key": "sk-secret", "count": 11}
```

Here `"api_key"` is a key and `"sk-secret"` is its value.

To look at every key in turn:

```python
for key in event_dict:
    print(key)          # "event", then "api_key", then "count"
```

To change a value, assign to it by key:

```python
event_dict["api_key"] = "***"
```

To check whether a word appears inside a key — `"key" in "api_key"` is `True`, because `in` on
two strings asks "does the second contain the first".

One trap worth knowing: changing a dictionary *while* looping over it can make Python complain.
If that happens, loop over a copy of the keys instead — `for key in list(event_dict):` — which
takes a snapshot first.

### The steps

1. **Predict first.** If your function does the redaction but forgets the `return event_dict` at
   the end, what do you think happens to the log line? Write it down, then try it. (It's more
   dramatic than you might expect.)

2. Write it. Decide which keys count as sensitive — `password`, `token`, `secret`,
   `authorization` are obvious starting points.

3. Prove it. Re-run the command above; `sk-this-should-not-be-here` should no longer appear.
   Then check you haven't broken anything: `count=11` should still show its real value.

4. **The judgement call.** What should happen with a key called just `key`? It's a perfectly
   ordinary English word — a dictionary key, a sort key, a cache key — and it's also the tail of
   `api_key`. Match too loosely and your logs fill up with asterisks where useful data should be;
   match too tightly and something leaks.

   Pick a rule, implement it, and be able to say why. There isn't one right answer, and the
   reasoning is the part I'll want to hear.

## Check yourself

1. Why is `log.info("documents_indexed", count=11)` better than an f-string, given both end up as
   text on a screen?
2. A processor is just a function. What must it take, and what must it return?
3. `psycopg` has never heard of structlog. How does its output end up as JSON with our fields on it?
4. What does `root.handlers.clear()` prevent?
5. Why is `configure_logging()` a function you call rather than code that runs on import?
6. What would break if `_redact_secrets` ran *after* the JSON renderer instead of before?

---

<details>
<summary>Answers</summary>

1. Because for most of its life it isn't text on a screen — it's a record something will filter,
   count or group. `count=11` is a number in its own field and can be summed; `"Indexed 11
   documents"` is a sentence you'd have to pick the number back out of, with something that
   breaks the moment anyone edits the wording.

2. It takes the logger, the method name, and the event dictionary; it returns the event
   dictionary. Everything in the chain has that shape, which is why you can insert your own
   anywhere in the line.

3. Logger names form a tree and everything travels up to the root logger. `configure_logging()`
   attaches one handler there, with a formatter that runs the structlog chain — so anything any
   library logs passes through the same processors ours does, and comes out the same shape.

4. Duplicate output. Call `configure_logging()` twice without it and you'd attach two handlers,
   so every line would be printed twice — a confusing bug, because nothing looks broken, you just
   get everything in duplicate.

5. Because importing a package shouldn't reconfigure the whole process's logging as a side
   effect. That decision belongs to whoever owns the program, so an entry point makes it
   explicitly. Same reasoning as `get_settings()` in lesson 3.

6. By then there'd be no dictionary left to redact — the renderer has turned it into a string.
   You'd be doing text substitution on JSON and hoping, rather than replacing a value by its key.
   This is why order in the chain matters.

</details>
