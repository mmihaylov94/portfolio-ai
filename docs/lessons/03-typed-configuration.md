# Lesson 3 — Typed configuration

## What you'll understand

- Why a Pydantic model is better thought of as a parser than as a container
- How to make "do not print this" a property of a value rather than something you remember
- Why the settings object is built by a function with a cache on it instead of just being a variable
- What `raise ... from` does, and why it matters when you translate one error into another
- Why a misconfigured process should refuse to start rather than fail on its first request

## Why it matters here

Nothing after this lesson works without settings. Lesson 6 needs a database URL, lesson 7 needs a
schema name, and the API eventually needs keys and a list of allowed origins. At the moment there
is no way to get any of that into the program at all.

It is also where two hazards particular to this project get their guard rails. The local database
and the production one differ by little more than a hostname and a port number, and picking the
wrong port produces an error that looks nothing like a wrong port. And this repository is public,
so a secret that finds its way into a log line is a secret you have to go and rotate.

Both of those are configuration problems. Which is convenient, because configuration is somewhere
you can fix a thing once instead of having to remember it everywhere.

## The concepts

Everything your program needs to know about where it is running — which database, whose API key,
how loudly to log — arrives from outside as text. Environment variables have no types.
`RETRIEVAL_TOP_K=20` is the string `"20"`. `DEBUG=false` is the string `"false"`, which is
perfectly truthy if you forget to convert it, and that particular bug has shipped to production in
every language that has ever had environment variables.

So there is a conversion step somewhere, whether you plan for one or not. This entire lesson is a
single decision about where to put it.

> Everything arriving from the environment is an untrusted string. Turn it into a typed, checked
> object exactly once, at startup — and afterwards the rest of the program can stop worrying
> about it.

The version without that decision is the one most projects drift into: read `os.environ` wherever
a value happens to be needed, convert it on the spot, move on. It works, and it costs you three
things quietly. The variable name gets retyped at every call site, so a typo in one of them is a
bug in one code path and nowhere else. Every caller does its own conversion, so one of them
eventually forgets. And a missing variable is discovered by whichever request first touches that
line, which might be days after the deployment that broke it.

Doing it once at the boundary fixes all three at the same time, and that is the only real idea
here. Everything below is a consequence.

### A model is a parser

The mental shift worth making early: a Pydantic model is not a container you put values into. It
is a function that takes messy input and either returns something known-good or refuses.

```python
class Settings(BaseSettings):
    retrieval_top_k: int = Field(default=20, ge=1, le=100)
```

That is not a declaration that `retrieval_top_k` happens to be an integer. It is an instruction:
take whatever arrived, make it an integer, check it sits between 1 and 100, and if any of that is
impossible then do not produce a `Settings` object at all.

Which means the payoff comes after the constructor returns. From that moment the value *is* an
int in the range you asked for, everywhere, forever. Nothing downstream needs to check it, convert
it, or handle the case where it is `None`. You spend the effort once, at the edge, and the
interior of the program gets simpler.

This is why the validators live here too. "Is this the right port?" feels like a question about
connecting to a database, but it is really a question about whether the configuration is sane, and
the time to ask it is while you are already inspecting the configuration.

### Secrets

An API key is a string, so the obvious type for it is `str`. We use `SecretStr` instead:

```python
openai_api_key: SecretStr
```

The difference is what happens when the value ends up somewhere you did not intend. With a plain
string it prints. With `SecretStr`, printing it gives you `**********`, and getting at the real
thing requires calling `.get_secret_value()` explicitly.

The point is not that this stops a determined attacker — anyone reading your code can call that
method. The point is that the *default* behaviour became safe. Log the whole settings object by
accident, dump it into an error report, include it in a debug print you forgot to remove, and the
key does not come with it. Meanwhile the one place that genuinely needs the value has to say so
out loud, which makes it visible in review.

That is a good pattern to recognise in general: when something is dangerous by default, changing
its type is usually a better fix than remembering to be careful.

### Closed sets

Two of the fields can only ever be one of a handful of values, and saying so is worth the couple
of extra characters:

```python
environment: Literal["local", "production"] = "local"
log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
```

Without `Literal`, `ENVIRONMENT=prod` is accepted happily and then quietly fails every
`if settings.environment == "production"` check in the codebase — the sort of bug that looks like
a logic error for an hour before you think to print the value. With it, the process stops at
startup and tells you what it expected:

```
ENVIRONMENT: Input should be 'local' or 'production'
```

Python also has `StrEnum`, which is worth reaching for once you need methods on the values, or
want to import them as symbols rather than repeat string literals. For two small fixed sets,
`Literal` says the same thing with less ceremony.

### Lists, and a gotcha

Environment variables have no notion of a list, so multiple values get packed into one string and
split apart on the way in. The obvious spelling does not work:

```python
cors_origins: list[str] = []      # SettingsError
```

pydantic-settings sees a complex type and tries to parse the value as JSON, so it expects
`["https://a.example"]` and falls over on a comma-separated string. The fix is to turn that
decoding off and do the splitting yourself:

```python
cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

@field_validator("cors_origins", mode="before")
@classmethod
def _split_comma_separated(cls, value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value
```

`mode="before"` is the part to notice. Validators normally run *after* Pydantic has coerced the
value to the declared type, which is too late here — there is nothing sensible to convert a
comma-separated string into. A "before" validator runs first, on the raw input, and hands back
something the normal machinery can then work with.

Two small details in there worth naming, since they are Python idioms you will see constantly.
`@classmethod` is required because the validator runs during class construction, before any
instance exists. And `default_factory=list` rather than `default=[]` avoids the mutable-default
trap, where every instance ends up sharing one list because the default is evaluated once when the
function is defined.

### Parse once, but lazily

The settings need to be built somewhere. The tempting version is a module-level variable:

```python
settings = Settings()      # don't
```

which runs the moment anything imports this module. That makes importing the package depend on the
environment being set up, which breaks the moment a test wants different values, or a tool imports
your code just to read its version number. Remember from lesson 2 that `__init__.py` runs on every
import — you would be making the whole package unimportable without a valid `.env`.

Instead:

```python
@lru_cache
def get_settings() -> Settings:
    ...
```

`lru_cache` remembers what a function returned for a given set of arguments. On a function that
takes no arguments there is only one possible call, so the first one does the work and every call
after that gets the same object back. Nothing happens until something asks, and then it happens
once.

```
same object: True
cache info : CacheInfo(hits=1, misses=1, maxsize=128, currsize=1)
```

It also gives tests a way out: `get_settings.cache_clear()` forces a re-read, which is exactly
what you want when a test needs to pretend the environment looks different.

### Failing at the boundary

Pydantic raises `ValidationError` when parsing fails. We catch it and raise our own error instead:

```python
try:
    return Settings()
except ValidationError as exc:
    raise ConfigError(_describe(exc)) from exc
```

Two reasons. The first is the `ConfigError` from lesson 2 finally getting a caller — anything that
wants to handle "we refused to start" can catch `PortfolioAIError` without also catching genuine
bugs.

The second is that the message gets translated. Pydantic reports the Python field name, but the
person reading the error has to go and fix an environment variable, so `_describe` upper-cases the
names and prints them as a list. It is a handful of lines that removes a small piece of friction
at a moment when nobody has patience for small pieces of friction.

The `from exc` is doing real work, and it is worth knowing about beyond this file. It records the
original exception as the cause of the new one, so the traceback shows both, joined by "The above
exception was the direct cause of the following exception". Leave it off and Python assumes the
second error happened *while handling* the first, which reads as an accident — and you lose the
Pydantic detail underneath your tidy summary. Whenever you catch one error and raise another, the
`from` clause is almost always what you meant.

## The code

### `config.py`

The whole file is one class, one helper and one cached function. The class body is mostly the
fields already discussed, so here is what surrounds them:

```python
model_config = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="forbid",
)
```

`env_file` points at the local file, which is gitignored and holds your real values. Real
environment variables take precedence over it, which is the behaviour you want in a container —
there the environment *is* the deployment, and the file usually is not there at all.

`extra="forbid"` is the interesting one. By default an unrecognised key in `.env` is ignored, so
this:

```
DATABSE_URL=postgresql://...
```

would be silently skipped, `database_url` would be reported as missing, and you would stare at a
file that visibly contains a database URL while being told there isn't one. Forbidding extras
turns it into something you can act on immediately:

```
Configuration is invalid:
  DATABASE_URL: Field required
  DATABSE_URL: Extra inputs are not permitted
```

Both halves of the mistake, named, side by side. The cost is that a key you stop using has to be
deleted rather than left lying around — which seems a fair trade for never losing twenty minutes
to a transposed letter.

Worth knowing precisely what this polices: the `.env` file and arguments passed directly. It does
*not* object to the wider OS environment, so `PATH` and everything else your shell defines are
unaffected. That is worth checking rather than assuming, and it is checked — if it policed the
whole environment, nothing would ever start.

`database_url` and `openai_api_key` have no defaults, which is deliberate. A default is a claim
that the program can sensibly carry on without the value, and neither of those qualifies. Missing
them should stop the process, not connect it to something plausible-looking.

### `.env.example`

Every key, with placeholders and no real values, because the repository is public. It is the file
someone clones and copies, so the comments in it are load-bearing — particularly the one about
which port has pgvector.

You will need to create your own `.env` from it. That one holds the real host and credentials, and
it is gitignored; check that it stays that way before you put anything real in it.

## Coming from PHP / Node

| | Python here | Laravel | Node |
|---|---|---|---|
| Source file | `.env` | `.env` | `.env` |
| Reader | `Settings` | `config/*.php` + `env()` | `process.env` |
| Access | `get_settings().db_schema` | `config('database.schema')` | `process.env.DB_SCHEMA` |
| Missing value | process will not start | `null` | `undefined` |
| Types | real, checked | strings | strings |

Laravel already pushes you towards the shape of this. The rule that you should only call `env()`
inside `config/` files, never in application code, exists so that config caching works — but the
effect is the same one we are after: read the environment at one boundary, and have the rest of
the app talk to something else. The difference is that Laravel's boundary hands you back
untyped values and `null` for anything absent, so the checking is still yours to do.

Node barely pretends. `process.env.PORT` is `string | undefined`, and the near-universal
`process.env.PORT || 3000` quietly papers over both "not set" and "set to something broken". If
you have ever shipped a container that silently ran on a default because a variable never made it
into the environment, that is the failure this lesson is designed to make impossible.

The thing to carry over, though, is not really about types. In all three languages you *can* reach
the environment from anywhere, and none of them will stop you. What makes this work is that there
is exactly one module allowed to do it. The discipline is the design; Pydantic just makes the
discipline cheap.

## Exercise

Write the validator that rejects a `DATABASE_URL` pointing at the wrong port.

`config.py` has a marked gap where it belongs. The background is in `CLAUDE.md`: locally, 5432 is
plain Postgres and 5433 is the one with pgvector. Point at 5432 and everything connects perfectly
happily, right up until the first migration fails with `type "vector" does not exist` — an error
that sends you off to debug a migration when the actual problem is four characters in a URL.

1. **Predict first.** Before writing anything, what do you expect Pydantic to do with a value a
   validator rejects? Does the exception come from the validator, or does Pydantic wrap it? What
   will `get_settings()` raise? Write your answer down, then find out.

2. Write the validator. `@field_validator("database_url")` with a plain `raise ValueError(...)`
   inside is enough — Pydantic collects those into its own error reporting.

3. Prove it works. A URL on 5433 should load; one on 5432 should be refused with a message that
   explains the actual problem, not just "invalid".

4. **Now the real question.** In production, 5432 is very likely the correct port — the container
   talks to Postgres on the standard one. A validator that rejects 5432 everywhere breaks the
   deployment on the day you first use it.

   Work out what the guard should actually do, implement it, and be able to say why. There is more
   than one defensible answer. What matters is that you noticed, because the obvious version of
   this validator is worse than no validator at all — it would pass every local test and fail only
   in production.

Leave your implementation in place when you are done. Unlike the last two exercises, this one is
real code the project keeps.

## Check yourself

1. Why is `openai_api_key` typed as `SecretStr` rather than `str`, given that anyone reading the
   code can still get the value out?
2. What would go wrong with `settings = Settings()` at the bottom of `config.py`?
3. `extra="forbid"` means a stale key in `.env` now breaks startup. What does that buy?
4. Why does the `cors_origins` validator need `mode="before"`?
5. What does `from exc` add, and what happens if you leave it out?
6. `database_url` has no default while `db_schema` does. What is the rule being applied?

---

<details>
<summary>Answers</summary>

1. Because it changes the default behaviour from unsafe to safe. Printing it, logging the settings
   object, or including it in an error report all produce `**********` instead of the key. Getting
   the real value requires calling `.get_secret_value()`, which is deliberate and visible in
   review. It is not protection against someone reading the code — it is protection against the
   accident.

2. It would run on import, so importing the package at all would require a valid `.env`. Tests
   wanting different values, and anything importing the code without a configured environment,
   would both break. The cached function defers the work until something actually asks.

3. It turns a typo from a silent no-op into an immediate, named failure. Without it, `DATABSE_URL`
   is ignored, `database_url` is reported missing, and you are looking at a file that plainly
   contains a database URL. With it, you get both halves of the mistake in the same message. The
   cost is having to delete keys you stop using.

4. Because a normal validator runs after Pydantic has coerced the value to the declared type, and
   there is no sensible way to coerce `"a, b"` into a `list[str]`. A "before" validator runs on the
   raw input and hands back something the normal machinery can work with.

5. It records the original exception as the cause of the new one, so the traceback shows both and
   says the first directly caused the second. Without it Python assumes the second error merely
   happened while handling the first, which reads like an accident, and the Pydantic detail
   underneath your summary is lost.

6. A default is a claim that the program can carry on sensibly without the value. That is true of
   the schema name, which has one obvious right answer. It is not true of the database URL — there
   is no sensible database to guess at, and starting up pointed at the wrong one is worse than not
   starting at all.

</details>
