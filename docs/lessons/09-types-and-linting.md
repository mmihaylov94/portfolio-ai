# Lesson 9 — Types and linting

## What you'll understand

- why a type annotation that is flatly wrong changes nothing when the program runs, and what
  mypy is therefore actually for
- why a type checker saying nothing is not the same as a type checker agreeing with you
- the difference between `Any` and `object`, and why only one of them is a promise
- when a linter finding means fix the code, when it means write down why not, and when it means
  the rule does not belong in this project
- why the rules in `pyproject.toml` were picked one at a time rather than switched on in bulk

## Why it matters here

Every function written in the last eight lessons carries type hints. Nothing has ever read them.
`pyproject.toml` has said so out loud since lesson 1:

```toml
# Tool configuration is deliberately minimal here; lesson 9 sets the real rules.
```

There is a more specific reason to do this now. In lesson 4 you were asked to predict what would
happen if `_redact_secrets` fell off the end without returning anything, given that it is
annotated `-> EventDict`. You said it would raise an error. It did not — the log line just became
`null`, and nothing anywhere complained. That is a completely reasonable prediction from PHP,
where a declared return type is checked as the function returns. This lesson is the answer to the
question it leaves behind: if the annotations do nothing, what are they for?

And lesson 10 needs this. CI gates the Docker build on ruff, mypy and pytest, and a gate is only
worth having if it can fail. Today mypy would fail on fifteen things nobody has looked at.

## The concepts

### The annotations have been decorative

Start with the smallest possible demonstration of the thing that surprised you. This function
promises a string and returns an integer, as obviously as it is possible to do:

```python
def double(n: int) -> str:
    return n * 2

result = double(21)
print(result, type(result))
```

Run it:

```
42 <class 'int'>
```

No error, no warning, nothing. Python read the `-> str`, stored it in an attribute nobody
consults, and carried on. You can even go and look at it while the program runs —
`double.__annotations__` is just a dictionary — but nothing in the language ever checks it against
what actually came back.

Now run mypy over the same four lines:

```
error: Incompatible return value type (got "int", expected "str")  [return-value]
```

So the annotation was not useless. It was just not being read by anybody. **And that is the whole
idea this lesson rests on:**

> Neither of these tools runs your code. They read it, and they only know what you actually
> wrote down.

Everything below follows from that sentence. It is worth keeping in mind, because the tools spend
most of their time telling you about things you thought were obvious, and the reason is always the
same: obvious to you, not written down anywhere.

### So a wrong annotation is silent until something reads it

There is one sitting in this repository right now, which you are going to find in the exercise, so
this is the shape of it rather than the location. A function that `yield`s is not a function that
returns — Python turns it into a generator, and calling it gives you a generator object rather
than running the body. If that function is annotated `-> None`, the annotation is wrong in a way
that has no effect whatsoever. The code works. The tests pass. The annotation is a sentence in the
file that says something untrue, and it will stay untrue until a second program reads it.

This is the part worth internalising, because it inverts something your instincts tell you. In
PHP a wrong return type is caught the moment the function returns, so the code tells you. In
Python the code will never tell you, so an unchecked annotation is not "mostly right" — it is
unverified, in exactly the way an untested function is unverified.

### Silence is not agreement

This is the most useful thing in the lesson, and it came out of actually running the tool rather
than reasoning about it.

When mypy was first pointed at this project it reported fifteen errors. Eight of them looked like
this:

```
src\portfolio_ai\db\pool.py:142: error: No overload variant of "__getitem__" of "tuple"
    matches argument type "str"  [call-overload]
```

That line is `row["search_path"]`. mypy is saying: you are looking a string up in a tuple, and
tuples do not work that way. Which is strange, because lesson 6 configured the pool with
`row_factory=dict_row` specifically so that rows come back as dictionaries, and the integration
tests prove at runtime that they do.

The cause is one line in `pool.py`, and it is a line that mypy never complained about:

```python
_pool: AsyncConnectionPool | None = None
```

`AsyncConnectionPool` is generic — it takes a type parameter saying what kind of connection it
hands out. This annotation does not supply one. Normally strict mode catches that immediately;
try it with a builtin and you get `Missing type arguments for generic type "list"`. But psycopg
declares its parameter like this:

```python
ACT = TypeVar("ACT", bound="AsyncConnection[Any]", default="AsyncConnection[TupleRow]")
```

That `default=` is a fairly new piece of Python typing, and it does exactly what it says. When you
leave the parameter out, you do not get an error. You get `AsyncConnection[TupleRow]` — tuple
rows — silently, because the library wrote down a sensible default and ours is not it.

So the pool was described as handing out tuples, the description was never questioned, and the
complaint surfaced five files away at the first line that treated a row as a dictionary. **Nothing
was wrong where the error was reported, and nothing was reported where the problem was.**

The fix is to write down what we actually knew:

```python
type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]
```

One line. It removed eight of the fifteen errors, across four files, and changed nothing at
runtime — the integration suite behaves identically before and after, which is the proof that it
was a description and not a change.

The lasting point is the one in the middle. A quiet type checker can mean your types are right. It
can also mean it filled in a blank you did not notice it filling.

### It believes you

There is a catch to that fix worth being honest about. Writing
`AsyncConnectionPool[AsyncConnection[DictRow]]` does not make the pool return dictionary rows.
`kwargs={"row_factory": dict_row}` does that, twenty lines further down, inside a plain dict where
no checker can see it. The annotation is a promise that the two agree, and mypy takes the promise
at face value without ever checking it.

Change the `row_factory` and forget the alias, and mypy will keep cheerfully approving
`row["status"]` for code that now returns tuples. That is not a flaw in mypy so much as the price
of the whole arrangement: a tool that only reads what you wrote down can only ever be as honest as
what you wrote down. It is why those two lines sit in the same file with a comment between them.

### `Any` and `object` look alike and are opposites

Another of the fifteen errors was this, on a test asserting something about an index definition:

```
error: Unsupported right operand type for in ("object")  [operator]
```

The helper it came from was annotated `-> list[dict[str, object]]`, which reads like a reasonable
way to say "a database row: string keys, values of some sort". It is not. Compare:

```python
def with_object(row: dict[str, object]) -> bool:
    return "hnsw" in row["indexdef"]      # error: Unsupported right operand type for in ("object")

def with_any(row: dict[str, Any]) -> bool:
    return "hnsw" in row["indexdef"]      # fine
```

`object` is the top of the class hierarchy, so it means *something, and I know nothing more about
it*. mypy is right to refuse: you cannot search inside a value that might be an integer. It is a
real, checked, maximally weak type.

`Any` means *stop checking here*. It is not a type so much as an instruction, and it spreads —
anything you pull out of an `Any` is also `Any`, and checking quietly stops for everything
downstream.

For a database row, `Any` is the truthful answer. psycopg genuinely cannot know what
`select indexdef from pg_indexes` returns; nobody can, statically. So the helper now says
`list[DictRow]`, which is psycopg's own name for `dict[str, Any]`, and the honesty is in using the
library's type rather than inventing a stricter-looking one that describes nothing.

That is the rule of thumb generally. `Any` where a value genuinely leaves the type system — a
database row, parsed JSON, a dynamic library boundary. `object` when you really do intend to hand
back something opaque. Never `Any` as a way to make an error go away, because it does not make the
error go away; it makes every error after it go away too.

### Libraries have to write it down as well

Two of the fifteen errors were mypy insisting that `Settings()` needs arguments:

```
error: Missing named argument "database_url" for "Settings"  [call-arg]
```

Which is, on the face of it, correct. `Settings` declares `database_url: str` with no default, and
nothing in that class definition says anything about environment variables. The fact that
pydantic-settings goes and reads the environment is behaviour hidden inside a base class, and no
amount of reading the signature would reveal it.

Some libraries solve this by shipping a plugin — a piece of code that teaches mypy how the library
behaves:

```toml
plugins = ["pydantic.mypy"]
```

With that one line both errors go, along with a third in the tests about `_env_file`. Plugins are
rare and slightly unsatisfying, but this is what they are for: a genuine gap between what the type
system can express and what the library does.

Most of the time the mechanism is duller and you have already used it. A library that wants its
annotations read ships a marker file called `py.typed`, which is the thing lesson 2 put into
`src/portfolio_ai/` without much explanation. Without it mypy refuses to look inside at all — it
reports the import as untyped and treats everything from that package as `Any`, so checking stops
at the import line however well annotated the code behind it is. A library with no annotations of
its own can still have them supplied separately, which is what `types-pyyaml` is doing in the dev
dependency group — stub files, nothing but signatures, for a library that ships none itself.

### `# type: ignore` takes a code, and that matters more than it looks

Sometimes you are right and the tool is wrong, and the escape hatch is a comment. It should always
carry the specific error code:

```python
result = thing()  # type: ignore[call-overload]
```

A bare `# type: ignore` silences every error on that line, including the ones that arrive next
year. The code narrows it to the one you actually examined.

There is a second half to this that is easy to miss. Strict mode turns on `warn_unused_ignores`,
so when an ignore stops being necessary, mypy tells you:

```
error: Unused "type: ignore" comment  [unused-ignore]
error: Incompatible return value type (got "MutableMapping[str, Any]", expected "dict[str, object]")
note: Error code "return-value" not covered by "type: ignore[arg-type]" comment
```

That is three lines about one comment in `test_logging.py`, and together they tell a small story:
the ignore was added for an `arg-type` error that no longer happens, and it was sitting on top of
a *different* error it was never silencing. The annotation underneath was wrong — the helper
promised `dict[str, object]` while structlog's processors deal in `EventDict`, which is a
`MutableMapping`. The fix was to use structlog's own type and delete the comment.

Suppressions rot. Being told when one has gone stale is most of what makes them safe to use.

### Now the linter, which knows even less

ruff is the same tool twice: a formatter, which you have been running since lesson 1, and a
linter, which has been running with almost nothing switched on.

The linter is a large pile of independent rules, grouped by the flake8 plugin each was ported
from. `E` is pycodestyle, `B` is bugbear, `S` is bandit's security checks, `PTH` wants `pathlib`
instead of `os.path`, and so on. You choose the groups. That choice is a genuine decision and not
a formality, so it is worth saying how this one was made.

The tempting option is `select = ["ALL"]` — turn everything on, subtract what annoys you. It goes
wrong in a specific way: the ignore list becomes the real configuration, nobody remembers why any
entry is in it, and it grows every time ruff ships a new rule, which means a routine dependency
bump can break CI with no change to the code. The alternative is more work once and quieter
forever: name the groups you want, one comment each. If nobody can write the comment, the group
does not go in.

Two dozen groups made it in. The security ones matter more than usual here — `S608` is
literally the project rule about never building SQL by string concatenation, now enforced rather
than remembered. `ASYNC` and `RUF029` went in because of lesson 5. They find nothing in this
codebase today, and both fire immediately on the mistake that lesson was about:

```
unused-async: Function `fetch` is declared `async`, but doesn't `await` or use `async` features.
blocking-sleep-in-async-function: Async functions should not call `time.sleep`
```

A rule that catches nothing is still worth having if it guards a trap you have personally fallen
into. It is worth being equally clear about what they do not catch: a sequential `await` inside a
loop is not flagged, and neither is a `gather` you forgot to await. They catch the blocking call
and the pointless `async`, not the general problem.

### Three things a finding can mean

Choosing the groups started by switching on far more than were wanted — most of ruff's catalogue,
just to see — which reported ninety-four violations. Sorting through them is the actual work,
and every one landed in one of three buckets — which is the same idea from the top of the lesson,
arriving from the other direction. A linter knows nothing about what your code is *for*, so a
finding is a question rather than a verdict, and there are three honest answers.

**The rule is right: fix the code.** A Yoda condition in a test, `os.path.dirname` nested three
deep where `Path(...).parents[2]` says the same thing once, a bare `5433` in a comparison that
deserved a name. These are small and the code is better afterwards.

**The rule is right in general and wrong here: write down why.** `global _pool` in `pool.py` is
discouraged for good reasons, and lesson 8 paid the price for it in the test fixtures — but the
alternative is threading a pool through every signature in the project for a benefit only the
tests would see. The processor in `logging.py` has two parameters it never uses, because the
signature belongs to structlog and every processor is called with all three. Both get a
suppression with the reasoning above it.

This is the same move as writing a type annotation. The comment is you writing down the thing the
tool could not know.

**The rule is wrong for this project: do not select it.** `TRY003`, `EM101` and `EM102` are one
opinion in three rules — that a `raise` must never contain its own message, so every message needs
a variable or a bespoke exception class. `raise ConfigError(_describe(exc))` is exactly what we
want that line to say. `TRY400` wants `log.exception` in every `except` block, which attaches a
traceback; `cli.py` suppresses the traceback deliberately for errors we raised ourselves. And
`ERA001` flags comments that look like commented-out code, which in a teaching repository full of
SQL fragments and shell lines is a false-positive generator.

Turning a rule off is a legitimate answer. Leaving it on and suppressing it in forty places is not.

## The code

### `pyproject.toml`

The ruff block starts with `preview = true`, which is needed for `RUF029` and part of `ASYNC`.
Preview rules can change between releases; the reason that is survivable is `uv.lock` — ruff is
pinned, so an upgrade is a commit somebody reviews rather than something that happens on a
Tuesday.

`select` lists two dozen groups with a comment each, `ignore` lists the four rejections above
with the reasoning, and `per-file-ignores` relaxes three rules for `tests/**`: `assert` is how a
test states its claim, the magic value in an assertion *is* the expected value, and tests import
private helpers on purpose because the private helper is the unit under test.

One surprise while writing this. In preview mode ruff prints rule *names* rather than codes, and
it has a rule asking you to do the same in your configuration and your suppressions. So the file
says `"raise-vanilla-args"` rather than `"TRY003"`, and suppressions read:

```python
global _pool  # ruff: ignore[global-statement]
```

rather than `# noqa: PLW0603`. It is worth knowing both spellings, because `# noqa` is the form
you will meet in every other Python codebase — it comes from flake8 and ruff still honours it. The
newer one was chosen here for one reason: a suppression that names the rule can be read without a
lookup, which matters more in a repository whose job is to be read.

The mypy block is shorter and does three things. `strict = true` switches on about a dozen flags
that mostly amount to "you did not write anything down here". `files = ["src", "tests",
"migrations"]` means bare `uv run mypy` checks the lot — eleven of the fifteen original errors were
in test code, which is where an unchecked annotation does the most damage, since a test that
cannot fail still passes. And `plugins = ["pydantic.mypy"]` for the reason above.

Worth noting what strict did *not* find: almost nothing in `src/`. Every function already had
annotations, so most of what strict adds had nothing to complain about. That is the useful result
rather than a compliment — it means the four errors it did find in `src/` were real disagreements
and not paperwork.

### Everything else

`db/pool.py` gets the `Pool` type alias and two suppressions. `config.py` gets
`_LOCAL_PGVECTOR_PORT = 5433` so the port appears once rather than twice. `logging.py` gets the
signature spread over three lines with a suppression on each unused parameter and a paragraph
explaining the protocol. `__init__.py` gets a file-level suppression, because the rule is about
the module rather than any one line in it. The tests get pathlib, structlog's `EventDict`,
psycopg's `DictRow`, and two stale `type: ignore` comments removed.

## Coming from PHP / Node

| | PHP | TypeScript | Python |
|---|---|---|---|
| Types checked at runtime | yes, with `strict_types` | no — erased at compile | no, ever |
| Can you run unchecked code | no | not through `tsc` | yes, always |
| Static analyser | PHPStan / Psalm | the compiler itself | mypy, separate and optional |
| Strictness dial | PHPStan levels 0–9 | `strict` in tsconfig | `strict = true` |
| "I give up" type | `mixed` | `any` | `Any` |
| "Something, unknown" type | — | `unknown` | `object` |
| Linter | PHP_CodeSniffer, PHPStan | ESLint | ruff |

The row that matters most is the second one. In TypeScript the type checker is the build step, so
code that does not type-check does not normally get run at all — the two are welded together. In
Python they are completely separate programs, and the one that runs your code has no interest in
the one that checks it. `uv run pytest` will happily run a file mypy refuses. That is why CI has
to run mypy explicitly in lesson 10; nothing else will ever do it for you.

If you have written TypeScript, the `Any` / `object` distinction is `any` versus `unknown` exactly,
including the reason `unknown` was added to TypeScript years after `any`: people kept reaching for
`any` to mean "I don't know what this is" and switching off type checking for everything
downstream by accident. Same trap, same shape, same fix.

Coming from PHP the harder adjustment is the first row. `declare(strict_types=1)` means a wrong
type is a `TypeError` where it happens, and that is a real safety net you no longer have. Python
gives you nothing at runtime and a very thorough analysis beforehand, if you run it. Which is the
lesson, really: the annotations were never doing anything on their own.

## Exercise

Two parts, about half an hour, and the repository should be clean at the end.

### Part A — the error a passing test cannot catch

Run the type checker:

```bash
uv run mypy
```

One error is left in the repository on purpose. Before you fix it:

1. Read it, and go and look at the function it names.
2. Write down, in one sentence, why all 42 tests pass despite it. Be specific — "because Python
   doesn't check types" is the right shape but not the answer; the question is what that function
   actually *is*, and why annotating it `-> None` was wrong rather than merely imprecise.
3. Predict what happens if you change nothing and run `uv run pytest -m integration`.

Then fix the annotation. The relevant type comes from `collections.abc`, and the file already
imports its sibling for the fixture above. `uv run mypy` should report no errors afterwards, and
the integration suite should still pass — if the behaviour changed, the fix was the wrong one,
because this is a description rather than a change.

### Part B — turn the async rules on and prove they work

`ASYNC` and `RUF029` are the carry-over from lesson 5. Add them to the `select` list in
`pyproject.toml` if they are not already there, then write this at the bottom of any file in
`src/`, from memory rather than by copying — it is the mistake lesson 5 was about:

- an `async def` that calls `time.sleep(1)` instead of awaiting anything
- a second `async def` that does something perfectly ordinary and synchronous

**Predict which rules fire on which function before you run anything.** There are two rules and
two functions, and it is not one each. Then:

```bash
uv run ruff check .
```

Delete both functions afterwards and confirm `uv run ruff check .` and `uv run mypy` are clean.

The prediction is the part that counts. It is checking whether the model from lesson 5 stuck, not
whether you can run a command.

## Check yourself

Six questions. Answers below, but they are worth attempting first — most of them come straight off
the one idea.

1. `double()` above is annotated `-> str` and returns an `int`. Why does Python not care, and
   where does the `-> str` actually go?
2. mypy never complained that `_pool: AsyncConnectionPool | None` was missing its type parameter.
   Why not, and what did that silence cost?
3. Adding `type Pool = AsyncConnectionPool[AsyncConnection[DictRow]]` fixed eight errors. Did it
   make the pool return dictionary rows? What would happen if `row_factory` were changed and the
   alias were not?
4. You have a function taking parsed JSON from an external API. Would you annotate the values
   `Any` or `object`? What changes for the caller either way?
5. `global _pool` gets a suppression rather than a code change. Argue the other side: what would
   satisfying the rule actually involve, and what would it cost? Is the suppression the right call
   here?
6. `ASYNC` and `RUF029` find nothing in this codebase. Make the case for keeping them, and then
   state the limit of what they cover.

<details>
<summary>Answers</summary>

**1.** Python stores annotations in `double.__annotations__` and never consults them. They exist
for other programs to read — mypy, IDEs, and libraries like Pydantic and FastAPI that genuinely do
use them at runtime, by looking them up deliberately rather than because the language does. The
language itself treats `-> str` as documentation it happens to keep a copy of.

**2.** Because psycopg declares the parameter with `default="AsyncConnection[TupleRow]"`, so
leaving it out is not an omission as far as mypy is concerned — it is an acceptance of the
default. The cost was eight errors reported in four files, none of them at the line that was
actually wrong, all describing tuples that no code in this project ever produces.

**3.** No. `kwargs={"row_factory": dict_row}` makes the pool return dictionaries; the alias only
describes it. If `row_factory` changed and the alias did not, mypy would keep approving
`row["status"]` on code that now returns tuples, and you would find out at runtime. The annotation
is a promise the checker takes on trust — which is exactly why the two lines live in the same file
with a comment between them.

**4.** `Any`. Parsed JSON genuinely leaves the type system — nothing can be known statically about
what an external API returned. With `Any` the caller can subscript, iterate and compare without
mypy objecting, and gets no protection; with `object` mypy rejects every one of those until the
caller narrows the type with an `isinstance` check. `object` is the stricter and more honest
choice if you intend to force that narrowing. For a response body you are about to hand to a
Pydantic model, forcing it is ceremony, and `Any` up to the model boundary is the usual answer.

**5.** Satisfying `global-statement` means the pool stops being module state: it gets created once
at startup and passed explicitly into everything that touches the database — every repository
function, every route, every CLI command, and the agent loop in between. That is genuinely better
in one respect, which lesson 8 ran into directly: the fixtures have to close and reset a global
between tests, and they would not if there were no global. The cost is a parameter on every
signature in the data layer, forever, for a benefit only the tests see. The suppression is the
right call for a project this size — and it is worth noticing that the *reason* it is right is
written down next to it, which is the only thing that separates a considered exception from
someone silencing an inconvenient rule.

**6.** They guard a mistake that has already been made once in this project, produces no error,
and shows up only as code that is slower than it should be — the worst possible failure shape,
since nothing tells you. That is worth a rule that never fires. The limit: they catch a blocking
call inside an `async def` and an `async def` that never awaits. They do not catch a sequential
`await` in a loop that should have been a `gather`, and they do not catch a `gather` whose result
is never awaited. The rules cover the two smallest versions of the trap, not the general one.

</details>
