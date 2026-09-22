# Lesson 10 — Docker and CI

## What you'll understand

- why the lockfile from lesson 1 is the thing that makes all of this possible, and what `--locked`
  adds that `uv sync` on its own does not
- why a Dockerfile installs dependencies before it copies the source, and what that has to do
  with how long a rebuild takes
- what a multi-stage build actually discards, and why that is a security decision as much as a
  size one
- why every fetched thing here is pinned — including GitHub Actions, which are pinned to a commit
  rather than a version
- why a secret in a Docker layer is permanent even if a later instruction deletes it

## Why it matters here

There are three commands you have been running for nine lessons:

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

Nothing makes anybody run them. They are a habit, and a habit is a thing you skip at half past
six on a Friday when the change is obviously fine. This lesson makes them a gate — the same three
commands, on a machine where skipping them is not an option, standing between a commit and
anything deployable.

The other half is the image. Everything so far has quietly depended on this one laptop: its
Python, its `.env`, its database on the LAN, even the `WindowsSelectorEventLoopPolicy` in
`__init__.py` that exists because of which operating system happens to be running the code. None
of that travels. The image is how the project stops being something that works here.

## The concepts

### One idea, two tools

> Everything in this lesson is about making the same thing happen on a machine that is not yours.

Docker answers it for the environment — the interpreter, the packages, the system libraries
underneath them. GitHub Actions answers it for the commands. Neither is complicated once you have
that sentence, and most of the individual decisions below are just it applied to something
specific.

The hinge is the lockfile. `uv.lock` pins every package and every transitive dependency to an
exact version and an exact hash, which is what makes "the same environment" a thing you can
actually ask for rather than approximately get. Lesson 1 committed it and the reason has been
theoretical until now.

There is one flag worth knowing about, and it is the difference between the lockfile helping and
the lockfile being decorative:

```bash
uv sync --locked
```

Plain `uv sync` will quietly re-resolve if `uv.lock` has drifted from `pyproject.toml` — helpful
on your own machine, and precisely wrong in a build. Without `--locked` a build can succeed with
dependency versions nobody has ever run, and the first anyone knows is a production error that
does not reproduce locally. With it, the build fails and says the lockfile is out of date. Both
the Dockerfile and the workflow use it, in every install step.

### An image is a stack of layers, and that is the whole thing

Every instruction in a Dockerfile produces a layer: a set of filesystem changes, stacked on the
ones before. Rebuilding reuses a cached layer if its inputs have not changed — and here is the
part that decides everything else, **if one layer is invalidated, every layer after it is too.**

So the ordering rule is simply: what changes least goes first. That single rule explains the
shape of the build:

```dockerfile
RUN --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY . /app

RUN uv sync --locked --no-dev
```

Dependencies are installed from `uv.lock` and `pyproject.toml` alone, before any source code
exists in the image. That layer is keyed on those two files, so editing a Python module leaves it
untouched and the rebuild skips straight past — sixty-odd packages, not reinstalled. Copy the
source first and you invalidate the dependency install on every single change, which turns a
ten-second rebuild into a two-minute one.

`--no-install-project` is what makes the split possible: install the dependencies, but not the
project itself, because the project is not there yet. The second `uv sync` installs it once the
source has arrived, and it carries one more flag that is easy to miss:

```bash
uv sync --locked --no-dev --no-editable
```

By default uv installs your own project in **editable** mode — the venv gets a pointer to
`src/`, not a copy of the code. That is what you want while developing, and a trap in an image,
because the venv then only works if the source tree ships alongside it at exactly the same path.
`--no-editable` copies the package in properly, and the result is a virtualenv that is
self-contained: the runtime stage needs no `src/` at all.

Two mounts are doing work in that first instruction and they are not the same thing.
`--mount=type=bind` makes a file available for the duration of one command without it becoming
part of a layer. `--mount=type=cache` keeps uv's download cache on the machine doing the building,
across builds, so a changed lockfile re-resolves without re-downloading everything it still needs.

### Multi-stage, and what actually gets thrown away

The build happens in one stage and the result is copied into another:

```dockerfile
FROM python:3.12-slim-bookworm AS builder
# ... uv, caches, the venv gets built here

FROM python:3.12-slim-bookworm AS runtime
COPY --from=builder --chown=app:app /app/.venv /app/.venv
```

The second `FROM` starts over from nothing. Only what is explicitly copied survives, so uv itself,
its caches, and any compiler toolchain that had been needed simply do not exist in the shipped
image. The size saving is real and it is the lesser benefit. The larger one is that build tools
are not attack surface: a compiler in a running container is a thing an attacker can use, and it
is doing nothing for you at that point.

This project gets off lightly, and for a reason from lesson 1. `psycopg[binary]` ships compiled
wheels, so there is no C extension to build and no `gcc` in the builder to leave behind. That was
a packaging decision made two months ago for convenience; it turns out to also be why the runtime
stage is clean.

There are two small pieces of Python-in-Docker that trip people up regardless of experience. The
first is `PATH`:

```dockerfile
ENV PATH="/app/.venv/bin:$PATH"
```

That is the whole of "activating" a virtualenv. There is nothing else to it, and no `activate`
script needs to run — putting the venv's `bin` first means `python`, `alembic` and `uvicorn`
resolve to the installed ones. The second is buffering. Python buffers stdout when it is not a
terminal, which in a container means logs arrive late, out of order, and not at all if the
process is killed — which is exactly the moment you want them. `PYTHONUNBUFFERED=1` turns that
off.

### Secrets, and why a layer is forever

A layer is an immutable set of filesystem changes. If one instruction copies `.env` into the
image and a later one deletes it, the file is gone from the final filesystem and **still sitting
in the earlier layer**, which anybody who can pull the image can extract. There is no undo,
because there is nothing to undo — the history is the image.

For this project that matters more than usual: the repository is public and the image goes to a
public registry, so "anybody" is not a figure of speech. Hence `.env` at the top of
`.dockerignore`, and configuration reaching the container as environment variables at run time —
which `Settings` was built for back in lesson 3.

`.dockerignore` is worth understanding as more than a tidiness file. Docker uploads the entire
build context to the daemon before it runs a single instruction, so anything listed there is a
file that is not sent, not hashed into any cache key, and cannot end up in a layer by mistake.
The other big entry is `.venv/`, which on this machine is full of Windows binaries that would be
useless in a Linux container and is rebuilt from the lockfile anyway.

### Cron in a container is not cron

The worker runs scheduled jobs, and the ordinary answer — the system `cron` daemon — is the wrong
one in a container. `cron` expects to be process 1, writes to syslog, wants to mail you the
output, and needs root. In a container that means logs land in a file inside a container nobody
opens.

[supercronic](https://github.com/aptible/supercronic) is a single binary that reads the same
crontab format, runs in the foreground as an ordinary user, and writes job output to stdout —
where Docker's logging picks it up, along with everything else this project emits as JSON. It is
a small thing, but it is the difference between a failed 4am ingestion appearing in your logs and
disappearing entirely.

### CI is the same commands, somewhere you cannot skip them

A workflow file describes **jobs**, each running on a fresh machine, each a list of **steps**.
Steps are either shell commands or **actions**, which are reusable bundles someone else wrote.
That is nearly all of the model.

Three jobs here. `quality` runs ruff, mypy and pytest with no database — fast, so it fails first
on the ordinary mistakes. `database` runs the integration tests against a real Postgres. `image`
builds the container, and declares `needs: [quality, database]`, which is what guarantees a red
build never produces a deployable tag.

A **service container** is how a job gets a database. GitHub starts the container alongside the
job and publishes a port to it:

```yaml
services:
  postgres:
    image: pgvector/pgvector:0.8.6-pg18
    options: >-
      --health-cmd pg_isready
      --health-interval 5s
```

The health check is not optional in practice. Without it the job races the database — the
container is reported as started long before Postgres accepts connections, and the first test
fails with a connection error that reads like a configuration problem and is not one.

### Pinning, and why actions get a commit rather than a version

Everything fetched in this lesson is pinned: the base image to `3.12-slim-bookworm`, uv to
`0.11.16`, pgvector to `0.8.6-pg18`, supercronic to a version *and* a checksum. Same reason each
time — a thing that can change underneath you is a difference between machines, which is what all
of this exists to eliminate.

Actions are pinned harder, to a commit SHA:

```yaml
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
```

A tag like `v7` is a label in someone else's repository, and they can move it onto anything at
any time. These actions run in the same job as a token that can push to our package registry, so
"whatever `v7` points at today" is not something to accept on trust. The comment is for humans;
the SHA is what runs. This is one of the more common real-world supply chain problems, and it
costs one line to not have.

The pgvector pin deserves its own sentence. It is `0.8.6-pg18` because that is exactly what the
development database runs. A suite that passes in CI against a different server than the one it
was written on is a slower way of finding out the same thing, and index behaviour — the part
lesson 8's test was written to protect — is precisely where versions differ.

## The code

### The guard collision

The first interesting thing that happened when this project ran somewhere new was that two
guards from earlier lessons disagreed about where they were.

`tests/integration/conftest.py` refuses to run unless `ENVIRONMENT` is `local`, because it creates
and drops schemas. `config.py` then insists that a *local* `DATABASE_URL` is on port 5433, because
locally 5432 is the plain Postgres without pgvector. Both are good rules. A stock pgvector service
container publishes 5432, so together they make CI impossible.

There were two honest ways out. Widen the `environment` field to accept a `ci` value and relax
both guards for it — more truthful about where the code is running, and it changes `config.py`,
the fixture, `.env.example` and two documents. Or publish the service on 5433:

```yaml
ports:
  - 5433:5432
```

One line, no code change, and it is not a fudge: `local` in both guards means "a database this
suite is allowed to create and drop schemas in", and a throwaway service container that is
deleted when the job ends is the purest possible example of one. The value describes the
permission, not the geography.

It is worth noticing what happened there, because it will happen again. Neither guard was wrong.
They were written months apart for different reasons and they had never been in the same room
before, and the first time they were, they contradicted each other. That is not a mistake anybody
made; it is what happens to assumptions that were never written down as assumptions.

### `docker/Dockerfile`

Two stages, as above. Three details in it are easy to get wrong:

**`migrations/` and `alembic.ini` are copied as files.** `pyproject.toml` declares
`packages = ["src/portfolio_ai"]`, so the installed package contains the code and nothing else —
the migrations are not in it and `--no-editable` does not change that. Without those two `COPY`
lines, `alembic upgrade head` inside the container finds no revisions and reports success, which
is worse than failing.

**`LICENSE` cannot be excluded by `.dockerignore`.** `pyproject.toml` has
`license-files = ["LICENSE"]`, so the build backend reads it when installing the project, and its
absence fails the second `uv sync` with a message about a missing licence file — nothing that
would make you think about `.dockerignore` at all.

**`UV_PYTHON_DOWNLOADS=never`.** Left to itself, uv may decide to download a managed Python
rather than use the one the base image already provides, and the image ships two interpreters.

The default `CMD` points at `portfolio_ai.api.main:app`, which does not exist yet. The image
builds; the command fails. That is deliberate — a placeholder that returns something harmless
would work perfectly and need remembering later, which is a worse outcome than a clear error.

### `docker/compose.yaml` and `docker/docker-compose.yml`

The local one exists to answer one question before CI does: does the thing we are about to push
start? It is not the development loop — that stays `uv run uvicorn --reload` on the host, because
editing a file and seeing the result is worth more than fidelity while you are building.

There is deliberately no Postgres service in either file. The database belongs to the environment,
not to this project; adding one here would mean a second database, a different version, an empty
schema and no relationship to anything the code is tested against.

The production file names no hosts, no addresses and no credentials, because it is committed to a
public repository. It joins the existing `traefik_proxy` network and carries **no Traefik labels**,
which is the interesting decision: the API is not on the internet at all. The only thing that
talks to it is the portfolio's Express API, from inside the network. Publishing it and then
guarding it with a bearer token would be two defences where one is needed, and the exposed one
would be the weaker.

### `.github/workflows/ci.yml`

Beyond the three jobs, four smaller things worth naming.

`concurrency` with `cancel-in-progress` stops a run when a newer commit lands on the same branch,
so a quick succession of pushes does not queue up runs of code nobody is waiting on.

`permissions: packages: write` is granted on the `image` job only. The other two jobs do not need
it and do not get it.

The GHCR login uses `secrets.GITHUB_TOKEN`, which GitHub issues for the run and expires when it
ends. There is no stored credential to leak or rotate.

`cache-from: type=gha` stores the layer cache in the Actions cache. Runners are fresh machines
with no local Docker state, so without it every build starts from nothing and the careful layer
ordering above buys precisely nothing.

That cache is also what makes the `Set up Buildx` step non-negotiable, and it is worth knowing why
because the error is actively misleading. A runner's default builder uses the plain `docker`
driver, which cannot export a build cache at all. Asking it to produces:

```
ERROR: failed to build: Cache export is not supported for the docker driver.
```

This was the first CI run's only failure, and the shape of it is the lesson. It fails *before the
first Dockerfile instruction runs*, so the message says nothing about the Dockerfile and the
Dockerfile is never reached — a red "Build image" job that has not built anything and tells you
nothing about whether it could. `docker/setup-buildx-action` creates a builder on the
`docker-container` driver, which can, and the two lines at the bottom of the job start working.

There is also a piece of YAML trivia here that is worth knowing because it will confuse you
elsewhere. Parse this file with a standard YAML library and the `on:` key comes back as the
boolean `True` — YAML 1.1 treats `on`, `off`, `yes` and `no` as booleans, which is the same rule
that turns the country code for Norway into `False`. GitHub's own parser handles it correctly, so
the workflow is fine, but it is the reason you will sometimes see `"on":` quoted in other
people's files.

## Coming from PHP / Node

| | PHP / Node | Python |
|---|---|---|
| Lockfile | `composer.lock`, `package-lock.json` | `uv.lock` |
| Strict install in CI | `composer install`, `npm ci` | `uv sync --locked` |
| Where dependencies live | `vendor/`, `node_modules/` | a virtualenv, anywhere on disk |
| Activating them | nothing to activate | put `.venv/bin` on `PATH` |
| Runtime in the image | `php:8.3-fpm`, `node:22` | `python:3.12-slim` |

The lockfile row should look familiar — `npm ci` versus `npm install` is exactly the distinction
`--locked` draws, for exactly the same reason.

The row that catches people is the fourth. `vendor/` and `node_modules/` are just directories in
the project; copy the project and you have copied them. A virtualenv is a directory *plus* a
`PATH` that points at it, and the second half is invisible. Most "it works locally but the
container can't find uvicorn" problems are one missing `ENV PATH` line.

One thing with no equivalent: the `python:3.12-slim` base image contains a full Python
interpreter, and `uv` may want to install *another* one. Node images do not have this problem,
because there is one Node. It is why `UV_PYTHON_DOWNLOADS=never` is in the Dockerfile.

## What is not verified yet

This lesson is the first one in the series that could not be fully run before it was written, and
saying so is better than writing confidently about output nobody saw. Docker is not installed on
this machine — the same fact that changed the testing strategy in lesson 8 — so the image has not
been built.

### Verified before the first push

- the unit tests pass with no `.env` anywhere on the path, so the `quality` job needs no
  configuration at all
- the integration tests pass with `ENVIRONMENT`, `DATABASE_URL` and a deliberately fake
  `OPENAI_API_KEY` supplied purely as environment variables — which is the exact configuration
  path the `database` job uses, both guards included
- `uv.lock` is in sync with `pyproject.toml`, so `--locked` will not fail
- every pinned artefact exists: the uv image tag, the pgvector tag, the supercronic release and
  its checksum, and every action SHA resolved to a real commit rather than a tag object
- all three YAML files parse

### What the first run settled

It ran. Two of the three jobs were green on the first attempt:

- **`quality` passed** — ruff, mypy and pytest, on a machine with no `.env` and none of this
  project's history.
- **`database` passed** — the service container came up with pgvector available, and the
  integration tests migrated into a throwaway schema and dropped it. Both guards from §"the guard
  collision" were satisfied by the port mapping, in the environment they were written for rather
  than the one they were tested in.

- **`image` failed**, on `Cache export is not supported for the docker driver` — the missing
  `setup-buildx-action` step described above. Which means **the Dockerfile is still unverified**:
  the job failed before executing a single instruction from it, so everything below is unchanged
  by that run.

Three small things came out of the same run and were fixed with it: the runners are now pinned to
`ubuntu-24.04` rather than `ubuntu-latest`, which is the argument this whole lesson makes applied
to the one thing it had missed; and `quality` and `database` now use distinct `cache-suffix`
values, because running in parallel with an identical dependency set made them race for the same
cache key and log `Unable to reserve cache` on every run. A warning that appears every time is a
warning nobody reads by the third week.

### Settled since, by pushing and then deploying

- **The image builds**, and GHCR accepts it: both tags appear, `latest` and `sha-<commit>`.
- **It runs.** The server pulls `latest` and runs ingestion in it on a schedule, and the deploy
  script runs `alembic current`, the migrations and a dry run inside the same image before
  starting anything.

### Still not verified

- that the layer cache behaves — the second build after a source-only change should skip the
  dependency install. Nobody has read the timings to check, which is the honest state of it.

```bash
docker build -f docker/Dockerfile -t portfolio-ai .
docker run --rm portfolio-ai python -c "import portfolio_ai; print(portfolio_ai.__version__)"
docker run --rm portfolio-ai alembic --version
```

That is the honest state of it, and it is also the argument for the whole lesson: the reason CI
exists is that "it works on my machine" is a claim nobody can check, including you.
