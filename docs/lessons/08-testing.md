# Lesson 8 — Testing

## What you'll understand

- Why a pytest test is just a function, with nothing to inherit and nothing to register
- What a fixture is, and why asking for one by name is all the wiring there is
- What fixture *scope* costs and buys
- Why an async test needs help, and what happens when it doesn't get it
- Why the singletons from lessons 3 and 6 are awkward here, and what that tells you

## Why it matters here

This is the first lesson where the pieces prove each other rather than accumulating. The tests run
the migrations from lesson 7, against the pool from lesson 6, using the settings from lesson 3. If
any of those were subtly wrong, this is where you'd find out.

It's also the lesson that decides whether you'll still be confident changing things in three
months. Everything so far has been small enough to hold in your head. That stops being true
somewhere around the ingestion pipeline, and tests are what replaces holding it in your head.

## The concepts

> A test is an ordinary function. pytest finds it by name, and fills its parameters by name.

That's the whole model. Everything below follows.

### Found by name

```python
def test_loads_with_defaults() -> None:
    settings = _settings()
    assert settings.db_schema == "portfolio_rag"
```

No class to inherit, no registration, no decorator. pytest looks for files called `test_*.py`, and
inside them functions called `test_*`. That's the entirety of discovery.

The plain `assert` is worth pausing on. It's Python's ordinary assert — and yet a failure gives
you:

```
E       assert 'portfolio_ragx' == 'portfolio_rag'
E         - portfolio_rag
E         + portfolio_ragx
```

pytest **rewrites assertions** as it imports your test files, replacing them with versions that
report both sides. That's why you never need `assertEqual`, `assertIn`, `assertGreater` and the
rest — one keyword covers all of it, and the failure message is better than any of them.

### Filled by name

A parameter on a test is a **fixture**:

```python
async def test_search_path_points_at_the_test_schema(test_schema: str) -> None:
    path = await current_search_path()
    assert path.startswith(test_schema)
```

Where did `test_schema` come from? A function of that name, decorated `@pytest.fixture`, in
`conftest.py`. pytest matched them by name, ran the fixture, and passed the result in.

That's dependency injection with no container and no configuration. Ask for a thing by naming it.

`conftest.py` is simply where fixtures live when more than one file needs them. pytest finds it
automatically — no import — and a `conftest.py` in a subdirectory applies only to that
subdirectory, which is how `tests/integration/conftest.py` gives database fixtures to integration
tests without unit tests ever knowing.

### Scope decides how often

A fixture rebuilds per test by default. Sometimes that's right; sometimes it's ruinous:

```python
@pytest.fixture(scope="session")
def test_schema() -> Iterator[str]:
    ...create a schema, run three migrations...
    yield schema
    ...drop it...
```

`scope="session"` means once per run, however many tests use it. Per test, this suite would run
three migrations before each of eight tests — slow enough that you'd stop running it, which is the
practical failure mode of a slow test suite.

The `yield` is the shape to notice: everything before it is setup, everything after is teardown,
and the teardown runs even if the test fails. Same structure as `with`, for the same reason.

### Markers select subsets

```python
pytestmark = pytest.mark.integration
```

A marker is a label. Ours separates tests that need a reachable Postgres from tests that need
nothing:

```bash
uv run pytest                  # unit only, 1.3 seconds, works offline
uv run pytest -m integration   # the rest
```

The default is unit-only, set in `pyproject.toml`. A suite that fails when you're on a train is a
suite you stop running, and a test suite you don't run isn't providing anything.

The split isn't about size or importance — it's about **speed and dependencies**. A unit test
touches nothing outside the process. An integration test needs something real.

### Async tests need help

pytest doesn't know what to do with `async def`. Left alone it would call the function, get a
coroutine back, and never await it — lesson 5's trap exactly, and the test would *pass*, because
nothing failed.

`pytest-asyncio` fixes that. We use `asyncio_mode = "auto"`, so every async test is awaited
automatically. The alternative is `@pytest.mark.asyncio` on each one — and forgetting it doesn't
fail, it silently reports a pass on a test that never ran.

That's a strong argument for `auto`: the failure mode of the explicit version is invisible.

## The code

### The singletons come due

Lesson 3 cached settings with `@lru_cache`. Lesson 6 made the pool a module-level global. Both are
convenient in a running program. Here's the bill:

```python
os.environ["DB_SCHEMA"] = schema
get_settings.cache_clear()
```

The settings object has already been built and cached by the time a test wants different values,
so the cache has to be cleared for the new one to be seen. `cache_clear()` exists because
`@lru_cache` provides it — lesson 3 pointed at it for exactly this.

The pool needs the same treatment, for a subtler reason:

```python
@pytest.fixture(autouse=True)
async def _reset_pool() -> None:
    yield
    await close_pool()
```

A pool opened during one test is bound to that test's event loop. Use it from the next test and
you get an error about a future attached to a different loop — which reads like a database
problem and isn't one. Closing it after each test removes the whole category.

`autouse=True` means every test in that directory gets it without asking. Some things shouldn't be
opt-in.

**This is worth being honest about.** Global state is convenient to write and awkward to test, and
the usual advice is to pass dependencies explicitly instead. That would mean threading a pool and
a settings object through every function in the project — a cost paid on every call, for a benefit
only tests see. For a project this size the fixtures win. On a larger one they might not, and it's
worth knowing which trade you've made rather than discovering it later.

### The database fixture

No Docker on this machine, so the testcontainers plan from lesson 1 was out. Instead each run gets
a throwaway schema on the Postgres you already have:

```python
schema = f"pytest_{secrets.token_hex(4)}"
os.environ["DB_SCHEMA"] = schema
get_settings.cache_clear()
command.upgrade(config, "head")
```

Three things make this work nicely.

**The migrations build it.** Not a separate "test schema" definition — the real migrations, run
into a fresh schema. A hand-maintained test schema drifts from the real one, and then tests pass
against a shape production never has.

**Schema creation comes free.** `migrations/env.py` already creates whatever schema it's pointed
at, because lesson 7 had to solve that chicken-and-egg for the version table. The fixture inherits
the fix.

**Nothing touches `portfolio_rag`.** The throwaway schema is dropped at the end, and orphans from
a crashed run are swept at the start of the next one.

There's a guard, too:

```python
if settings.environment != "local":
    pytest.exit("Refusing to run integration tests with ENVIRONMENT=...")
```

This fixture creates and drops schemas. The cost of it running somewhere unexpected is high enough
that a guard clause is cheap insurance.

### What's tested, and what isn't

The unit tests cover the things you wrote: the port guard from lesson 3, the redaction processor
from lesson 4, `gather_limited` from lesson 5. Each one is now protected against a future change
that breaks it quietly.

Two are worth singling out.

**The production case of the port guard.** `test_production_accepts_the_standard_port` is the test
that makes the guard *correct* rather than merely strict — a validator rejecting 5432 everywhere
passes every local test and breaks the deployment.

**The over-matching cases for redaction.** `monkey`, `keyboard_layout`, `turkey_recipe` are all
checked to be left alone. Under-matching leaks a secret; over-matching fills the logs with
asterisks and gets redaction switched off entirely. The second failure is less dramatic and more
likely.

And the integration tests check behaviour rather than definitions:

```python
async def test_chunks_cascade_when_a_document_is_deleted() -> None:
```

Checking the constraint exists would prove the migration said the words. Inserting a document and
a chunk, deleting the document, and finding the chunk gone proves the database agrees.

### Two things that went wrong writing this

**A coroutine that was never awaited.** The limit-rejection test was originally:

```python
await gather_limited(limit, _double(1))
```

It passed, and emitted:

```
RuntimeWarning: coroutine '_double' was never awaited
```

`gather_limited` raises on a bad limit *before* awaiting anything, so that coroutine was created
and discarded. Lesson 5's point arriving uninvited: calling an async function makes something that
has to be awaited or explicitly thrown away. The fix was to pass no awaitable at all.

**An f-string in SQL, in the test suite.** One test read the Alembic version like this:

```python
f'select version_num from "{test_schema}".alembic_version'
```

The value is one we generated, so it's safe — and it's still the exact pattern lesson 6 spends a
page arguing against. Ruff flagged it via an unused `noqa` I'd added to silence a rule that isn't
enabled yet, which is its own small lesson about suppressing warnings.

The real fix was better than quoting it properly: the search path already points at the test
schema, so the qualification was unnecessary. The query is now `select version_num from
alembic_version` and resolves correctly because of lesson 6's design.

## Coming from PHP / Node

| | pytest | PHPUnit | Jest / Vitest |
|---|---|---|---|
| A test | `def test_x()` | method in a `TestCase` | `test('x', () => {})` |
| Assertions | plain `assert` | `$this->assertEquals(...)` | `expect(a).toBe(b)` |
| Setup | fixtures, by parameter | `setUp()` | `beforeEach` |
| Sharing setup | `conftest.py` | base classes / traits | imports |
| Selecting | markers, `-m` | groups | `describe` / `.only` |

**The fixture model is the real difference**, and it's worth unlearning some instincts for.

PHPUnit gives you `setUp()`, which runs before every test in the class and sets `$this->` state.
Sharing it means a base class, and tests that need different setups mean more base classes.
pytest tests declare what they need *individually* — one test in a file can take `test_schema`,
another can ignore it and never pay for it.

Jest's `beforeEach` is closer in spirit, but it's still positional: it applies to the block, not
to the test that asked. There's no equivalent of a test requesting one specific thing by name.

**And the assertion difference is bigger than it looks.** `assertEquals`, `assertSame`,
`assertContains`, `assertGreaterThan` — a vocabulary to learn. pytest has one keyword, because it
rewrites your assertion to produce the diff. Less to remember, better output.

## Exercise

Write the test that proves the migration created the `vector` extension and the HNSW index.

It's the last test in `tests/integration/test_migrations.py`, currently skipped.

### Why this test is worth having

Lesson 7 established that an index built for the wrong operator class isn't rejected — it's
**silently ignored**. The query returns correct results and reads every row in the table. No error,
no warning, and the symptom is slowness arriving long after the change that caused it.

Nothing except a test will catch that.

### What to check

1. The `vector` extension is installed. Near enough a formality — `chunks` couldn't exist without
   it — but it documents the dependency.
2. There's an HNSW index on `chunks.embedding`, and it uses `vector_cosine_ops`.

### Useful starting points

```sql
select extname from pg_extension

select indexdef from pg_indexes where schemaname = %s and tablename = 'chunks'
```

`indexdef` gives you the `CREATE INDEX` statement as text, which contains both the access method
(`hnsw`) and the operator class (`vector_cosine_ops`). `_fetch_all` at the top of the file is the
helper the other tests use, and `test_schema` is available as a parameter.

Remove the `pytest.skip(...)` line when you're done, then:

```bash
uv run pytest -m integration
```

### Then break it on purpose

Once it passes, prove it actually tests something. Change the migration's index to
`vector_l2_ops`, run `alembic downgrade base && alembic upgrade head` against your dev database,
and confirm the test fails. Then put it back.

A test you've never seen fail is a test you're trusting on faith.

## Check yourself

1. How does pytest know `test_schema` should be passed to a test that asks for it?
2. Why does a plain `assert a == b` give a useful failure message?
3. What would go wrong if the `test_schema` fixture had default scope?
4. `asyncio_mode = "auto"` — what's the failure mode of the alternative?
5. Why does `_reset_pool` close the pool after *every* test rather than once at the end?
6. Why do the migration tests run the real migrations instead of a test-specific schema definition?

---

<details>
<summary>Answers</summary>

1. By name. A parameter on a test is looked up among the fixtures pytest knows about — functions
   decorated `@pytest.fixture`, found in the test file or a `conftest.py` above it. There's no
   registration or import; matching the name is the whole mechanism.

2. Because pytest rewrites assertions as it imports your test files, replacing them with code that
   reports both sides and computes a diff. That's why there's no `assertEqual` — one keyword
   covers everything and produces better output than a method name could.

3. It would create a schema and run three migrations before *every* test. With eight tests that's
   twenty-four migration runs instead of three, making the suite slow enough that you'd stop
   running it — which is the real cost of a slow suite.

4. Forgetting `@pytest.mark.asyncio` doesn't fail. pytest calls the function, gets a coroutine,
   never awaits it, and reports a pass on a test that never ran. An invisible failure mode, which
   is the strongest argument for `auto`.

5. Because a pool is bound to the event loop that created it. Each test gets a fresh loop, so a
   pool carried over produces an error about a future attached to a different loop — which reads
   like a database fault rather than a lifecycle one.

6. Because a separate test schema definition drifts from the real one, and then tests pass against
   a shape production never has. Running the actual migrations means the thing under test is the
   thing that ships.

</details>
