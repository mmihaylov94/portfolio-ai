# Lesson 1 — Tooling and uv

## What you'll understand

- What a virtual environment actually *is*, and why Python needs one where Node does not
- What `pyproject.toml` declares, and how it differs from `uv.lock`
- Why dependency **groups** are not the same as optional **extras**
- Why an application — not just a library — needs a `[build-system]`
- The difference between `requires-python` and `.python-version`, which look interchangeable and are not

## Why it matters here

Everything after this lesson runs through `uv run`. Ingestion, the API, migrations, tests and the
eval harness are all commands executed inside the environment this lesson creates, and the
Dockerfile in lesson 10 installs from the lockfile this lesson generates. Get the mental model
right once and the rest of the project stops being mysterious.

There is also a specific reason to care about reproducibility here: this project talks to a real
database and spends real money on OpenAI calls. "Works on my machine" is not an acceptable
property of a thing that can issue `DELETE` statements against a knowledge base.

## The concepts

### What a virtual environment really is

Python has one global `import` namespace per environment. When you write `import openai`, the
interpreter searches a list of directories (`sys.path`) and takes the first `openai` it finds.
There is no mechanism for two versions of a package to coexist in one environment — the name
resolves to exactly one thing.

That is the whole reason virtualenvs exist. A venv is not clever; it is a directory:

```
.venv/
├── pyvenv.cfg          # points at the base interpreter that created it
├── Scripts/            # "bin/" on macOS and Linux — python.exe, pip.exe, and any tool entrypoints
└── Lib/site-packages/  # where installed packages actually land
```

"Activating" a venv does almost nothing: it prepends `Scripts/` to your `PATH` so that `python`
resolves to the venv's interpreter. That interpreter, because of where it lives, computes a
`sys.path` pointing at its own `site-packages` rather than the system one. That's it. There is no
magic, no daemon, no registry.

This matters because it explains the failure mode everyone hits once: you install something, then
run `python` from a shell where the venv is not active, and the import fails. Nothing is broken —
you are simply talking to a different interpreter with a different `sys.path`.

**uv sidesteps the whole problem.** `uv run <command>` locates `.venv`, makes sure it matches the
lockfile, and runs the command inside it. You never activate anything. Every command in this
project is written as `uv run ...` for that reason.

### `pyproject.toml` — one manifest for everything

Python used to scatter project metadata across `setup.py`, `setup.cfg`, `requirements.txt`,
`MANIFEST.in`, `.flake8` and more. `pyproject.toml` (standardised as PEP 621) replaced the lot.
One file now holds project metadata, dependencies, the build backend, and the configuration for
most tools.

The three blocks that matter in ours:

```toml
[project]                 # PEP 621 metadata: name, version, requires-python, dependencies
[dependency-groups]       # PEP 735 dev-only dependencies
[build-system]            # how to turn this source tree into an installable package
```

Plus `[tool.<name>]` sections, where individual tools keep their config — `[tool.ruff]`,
`[tool.mypy]` and so on. That is a convention, not a standard: each tool decides whether it reads
from `pyproject.toml` at all.

### Declarations versus the lock

Two files, two jobs, and conflating them is the most common beginner mistake:

| | `pyproject.toml` | `uv.lock` |
|---|---|---|
| Written by | you | uv |
| Contains | *intent* — "some openai ≥ 1.60" | *fact* — "openai 3.16.2, hash sha256:…" |
| Includes transitive deps | no | yes, all of them |
| Read when | resolving | installing |
| Committed | yes | yes |

You declare loose floors (`>=`) and let the lock pin exact versions. Pinning exact versions in
`pyproject.toml` as well is a common instinct and an actively bad one: it makes upgrades a manual
edit rather than a `uv lock --upgrade`, and it produces unresolvable conflicts the moment two
dependencies disagree.

`uv.lock` has a property `requirements.txt` never had: it is **universal**. It records the
resolution for every platform and Python version the project supports, not just the one you
happened to run on. A lockfile generated on Windows installs correctly in a Linux container — the
exact thing this project does when the Docker image is built in CI.

### Groups are not extras

Both look like "optional dependencies", and they are completely different:

- **Extras** (`[project.optional-dependencies]`) are part of your published package. Someone can
  `pip install portfolio-ai[postgres]`. They ship.
- **Groups** (`[dependency-groups]`) are for local development. They are never published and a
  consumer of your package cannot install them.

`ruff`, `mypy` and `pytest` belong in a group. They are tools for working *on* the project, not
things the project needs to run. That distinction is what keeps the production Docker image from
shipping a test framework.

### Why an application needs a build backend

`[build-system]` looks like something only libraries need. It is here because uv installs *this
project itself* into the venv, in editable mode. That is what makes this work from any directory:

```python
from portfolio_ai.config import settings
```

Without it you would be relying on the current working directory being right, and on
`src/` somehow being on `sys.path`. Both are fragile, and both break the moment a test runner or
a Docker `CMD` starts the process from somewhere unexpected.

"Editable" means the venv contains a pointer to `src/portfolio_ai`, not a copy. Edit a file and
the next import sees the change — no reinstall. Lesson 2 goes into why the code lives under
`src/` at all.

### Two files that both say "3.12"

This trips people up, so be explicit about it:

- **`requires-python = ">=3.12"`** in `pyproject.toml` is a *constraint*. It tells the resolver
  which Python versions the project must work on, and it is what makes the lock universal across
  3.12, 3.13 and later.
- **`.python-version`** is a *selection*. It tells uv which interpreter to actually use when
  creating `.venv` here. It is a local development detail and has no effect on anyone else.

One says "must work on at least this"; the other says "use exactly this, here, now".

## The code

### `.gitignore`

Secrets are first in the file and first in importance, because **this repository is public**:

```gitignore
.env
.env.*
!.env.example
```

The third line is the interesting one. `!` un-ignores a pattern, so `.env.local` and `.env.prod`
are excluded while the committed template survives. Order matters — a later rule overrides an
earlier one.

`.venv/` is ignored because it is a build artifact: hundreds of megabytes, platform-specific, and
fully reconstructible from `uv.lock` with one command. Committing it is the Python equivalent of
committing `node_modules/`.

### `.python-version`

One line: `3.12`. uv reads it when creating the environment.

### `pyproject.toml`

Dependencies are grouped by role with comments, because a bare alphabetical list of twelve
packages tells a reader nothing:

```toml
dependencies = [
    # LLM + embeddings
    "openai>=1.60",
    # Postgres: the driver, its async connection pool, and pgvector type adapters
    "psycopg[binary,pool]>=3.2",
    "pgvector>=0.3",
    ...
]
```

Two details worth pausing on.

**`psycopg[binary,pool]`** — the square brackets are extras, as described above. `binary` pulls a
precompiled build so there is no C compiler needed at install time; `pool` adds the connection
pool that lesson 6 uses. Without the extras you get the bare driver and a build step.

**Everything is declared now, not lesson by lesson.** We know the full dependency set from
`ARCHITECTURE.md`, so the lockfile is the project's real lockfile from the start, and later
lessons are about code rather than dependency admin.

The tool sections are deliberately minimal:

```toml
[tool.ruff]
line-length = 100
target-version = "py312"
```

Lesson 9 configures linting and type checking properly. This is just enough that the tools do not
disagree about formatting in the meantime.

### `src/portfolio_ai/__init__.py`

Three lines, and it exists mainly so the build backend has a package to install. Lesson 2 explains
what `__init__.py` does and why the `src/` layout is worth the extra directory — ignore it for now.

### What `uv sync` did

It resolved the dependency graph, wrote `uv.lock` (~379 KB, mostly hashes), created `.venv`,
installed everything into it, and installed this project in editable mode. Verify:

```bash
uv run python -c "import portfolio_ai; print(portfolio_ai.__file__)"
```

The path printed is your `src/` directory, not a copy inside `site-packages` — that is the
editable install working.

If you want to see the mechanism rather than trust it, look inside the venv:

```bash
ls .venv/Lib/site-packages/ | grep portfolio
# _editable_impl_portfolio_ai.pth
# portfolio_ai-0.1.0.dist-info
```

A `.pth` file is a plain text file that Python reads at startup and uses to extend `sys.path`.
There is no copy of your code in `site-packages` — just a pointer back to `src/`. "Editable
install" sounds like a special mode; it is a text file with a path in it.

While you are in there, `cat .venv/pyvenv.cfg` shows the `home = ...` line pointing at the base
interpreter that created the environment. That one line is what makes a venv a venv.

## Coming from PHP / Node

| Python | Node | PHP |
|---|---|---|
| `pyproject.toml` | `package.json` | `composer.json` |
| `uv.lock` | `package-lock.json` | `composer.lock` |
| `[dependency-groups] dev` | `devDependencies` | `require-dev` |
| `uv run <cmd>` | `npm run` / `npx` | `composer exec` |
| `uv add <pkg>` | `npm install <pkg>` | `composer require` |
| `.venv/` | `node_modules/` | `vendor/` |

The table makes it look like a straight translation. One difference is real and worth internalising:

**npm can install two versions of the same package; Python cannot.** Node resolves modules by
path, so nested `node_modules` directories let `a` and `b` each have their own copy of `lodash`.
Python resolves by name on a flat `sys.path`, so one environment has exactly one `openai`. When
two dependencies demand incompatible versions of a third, npm shrugs and Python fails to resolve.

This is why Python's resolvers are stricter and occasionally refuse to install things, and why
"just add the package" sometimes isn't possible. It is not uv being difficult — it is the import
system being flat.

Second difference: **`.venv` contains an interpreter, not just libraries.** `vendor/` and
`node_modules/` hold code; `.venv/` holds code *and* the Python that runs it. That is why the venv
is tied to one Python version and why `.python-version` exists at all.

## Exercise

Add a dependency to the dev group and read what changes.

1. Add `pytest-cov` (a coverage plugin for pytest) to the dev group:

   ```bash
   uv add --group dev pytest-cov
   ```

2. Look at what changed:

   ```bash
   git diff pyproject.toml
   git diff --stat uv.lock
   ```

3. Answer these, in your own words:
   - `uv.lock` gained more than one package. Which ones, and why, given you asked for one?
   - Why is `pytest-cov` in `[dependency-groups]` rather than `[project.dependencies]`? What would
     go wrong in lesson 10's Docker image if it were in the wrong place?
   - `uv.lock` is a generated file. Argue for committing it in one sentence, then argue against —
     the case against is weaker, but it is not empty, and knowing why sharpens the first answer.

4. Then remove it again — we are not using coverage yet:

   ```bash
   uv remove --group dev pytest-cov
   ```

   Confirm `git status` is clean afterwards. If it is not, work out what did not get reverted.

## Check yourself

1. You run `python -c "import openai"` in a terminal and get `ModuleNotFoundError`, but
   `uv run python -c "import openai"` works. What is different?
2. What is the difference between `requires-python` and `.python-version`?
3. Why is `uv.lock` committed when `.venv/` is not, given both are generated?
4. Why does an application need `[build-system]`?
5. Why can npm install two versions of one package when uv cannot?

---

<details>
<summary>Answers</summary>

1. The bare `python` is whichever interpreter is first on your `PATH` — probably the system one,
   whose `sys.path` points at a different `site-packages` that has no `openai`. `uv run` executes
   the command with `.venv`'s interpreter instead. Nothing is broken; they are two different
   Pythons.

2. `requires-python` is a constraint on which Python versions the project supports, used during
   resolution and by anyone installing it. `.python-version` selects which interpreter uv uses to
   build the local `.venv`. Constraint versus selection.

3. `uv.lock` is small, text, and the only record of exactly which versions were resolved — it is
   the reproducibility guarantee, and a diff on it is reviewable. `.venv/` is large, binary,
   platform-specific, and fully reconstructible from the lock in one command. Commit the recipe,
   not the cake.

4. Because uv installs the project itself into the venv in editable mode, which is what makes
   `import portfolio_ai` work regardless of the current working directory. Without it you would
   depend on `src/` landing on `sys.path` by luck.

5. Node resolves modules by filesystem path and allows nested `node_modules`, so two copies can
   coexist at different paths. Python resolves by name on a flat `sys.path`, so a given
   environment has exactly one package of a given name — a conflict is a hard failure rather than
   a duplication.

</details>
