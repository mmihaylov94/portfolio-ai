# Portfolio AI -- one image, several entrypoints.
#
# The same image runs the API and the scheduled jobs; the command decides which.
# Building two images from one codebase would mean two things to keep in step and
# two chances for them to drift.
#
# Built from the repository root, not from this directory:
#
#     docker build -f docker/Dockerfile -t portfolio-ai .
#
# The build context has to include pyproject.toml, uv.lock and src/, and Docker
# cannot reach above the context root, so the context is the project and the
# Dockerfile points into it.

# ---------------------------------------------------------------------------
# Stage 1: build the virtualenv
# ---------------------------------------------------------------------------
# Pinned to a patch version rather than 3.12-slim. The floating tag would move
# under us at some point between a green CI run and a deploy, which is the exact
# class of surprise this whole lesson is about avoiding.
FROM python:3.12-slim-bookworm AS builder

# uv is a single static binary, so it is copied in from its own published image
# rather than installed with pip. Pinned to the version in use locally: the build
# and the laptop should resolve the lockfile with the same program.
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /uvx /bin/

# Compile .pyc files during the build. It makes the image slightly larger and the
# first request measurably faster, which is the right trade for a long-running
# server that is built once.
ENV UV_COMPILE_BYTECODE=1
# Copy packages into the venv rather than hardlinking them. The uv cache lives on
# a mount and the venv does not, so they are on different filesystems and
# hardlinks are not possible.
ENV UV_LINK_MODE=copy
# Use the interpreter this image already has. Left alone, uv may decide to
# download a managed Python of its own, and the image would then ship two.
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, project second. This is the whole layer-caching idea: the
# dependency layer is keyed on uv.lock and pyproject.toml, so editing a Python
# file leaves it untouched and the rebuild skips straight past it. Copying the
# source in first would invalidate the dependency install on every single change.
#
# --mount=type=bind puts the two files in place for this one command without
# adding them to a layer. --mount=type=cache keeps uv's download cache between
# builds, on the machine doing the building.
#
# --locked is the flag that matters. It fails if uv.lock does not match
# pyproject.toml, rather than quietly re-resolving and building an image with
# dependency versions nobody has ever tested.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Now the project itself. LICENSE has to be here as well as the source, because
# pyproject.toml declares license-files = ["LICENSE"] and the build backend reads
# it -- .dockerignore excluding it would fail this step with a message about a
# missing licence file rather than anything about Docker.
COPY pyproject.toml uv.lock LICENSE README.md ./
COPY src/ ./src/

# --no-editable matters here. By default uv installs the project in editable
# mode, which puts a pointer to /app/src into the venv rather than the code
# itself -- convenient while developing, and a trap in an image, because the venv
# then only works if src/ is shipped alongside it at exactly the same path.
# --no-editable copies the package into the venv properly, so the venv is
# self-contained and the runtime stage needs no source tree at all.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---------------------------------------------------------------------------
# Stage 2: the image that ships
# ---------------------------------------------------------------------------
# Everything above -- uv, the build caches, the compiler toolchain if there had
# been one -- is discarded here. Only what is explicitly copied survives, which
# is what "multi-stage" buys: build tools do not become attack surface.
FROM python:3.12-slim-bookworm AS runtime

# supercronic rather than the system cron daemon. cron expects to be init, writes
# to syslog, and needs root; supercronic is a single binary that runs in the
# foreground as an ordinary user and writes job output to stdout, which is where
# Docker collects logs from. Running the schedule under cron means the logs end up
# in a file inside a container nobody looks in.
#
# The checksum is the point of the three lines rather than the download: it is a
# binary fetched over the network at build time, and it should be the one that was
# reviewed.
ARG SUPERCRONIC_VERSION=v0.2.48
ARG SUPERCRONIC_SHA1SUM=016b7c9aebfc8d9fd9526e8ba33b191fc524485f
ADD --chmod=755 \
    https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64 \
    /usr/local/bin/supercronic
RUN echo "${SUPERCRONIC_SHA1SUM}  /usr/local/bin/supercronic" | sha1sum -c -

# A user with no password, no shell and no reason to exist outside this process.
# Containers run as root by default, and a process that only ever reads its own
# code and talks to Postgres has no use for it.
RUN useradd --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# The virtualenv, built in the stage above, with its ownership set as it lands.
# Copying and then chown-ing would double the size of the layer, since the second
# command rewrites every file it touches.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

# No src/ here: the package is inside the venv, properly installed, because the
# builder used --no-editable.
#
# Alembic is a different matter. pyproject.toml ships src/portfolio_ai only, so
# migrations/ and alembic.ini are not part of the installed package and have to
# travel as ordinary files -- without them `alembic upgrade head` in this
# container finds no revisions and cheerfully reports success.
COPY --chown=app:app migrations/ ./migrations/
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app docker/crontab /etc/crontab

# Put the virtualenv first on PATH. This is what "activating" a venv does; there
# is nothing else to it, and no activate script needs running. `python`,
# `alembic` and `uvicorn` now resolve to the ones installed above.
ENV PATH="/app/.venv/bin:$PATH"
# Never buffer stdout. Buffered logs arrive late, out of order, and not at all if
# the process is killed -- which is exactly when they matter.
ENV PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

# The API. `python -m portfolio_ai.api` rather than the uvicorn command, so the
# server's settings live in one place in code and every log line is JSON from the
# first (see src/portfolio_ai/api/__main__.py). The worker overrides this with its
# own command in docker-compose.yml.
#
# No HEALTHCHECK here, although the API has one: the worker runs this same image
# and serves no HTTP, so an image-level check would mark it unhealthy forever. The
# check is declared on the api service in docker-compose.yml instead.
CMD ["python", "-m", "portfolio_ai.api"]
