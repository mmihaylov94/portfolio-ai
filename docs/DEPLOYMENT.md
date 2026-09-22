# Portfolio AI — Deployment

How this project is deployed to the server that already runs [mihaylov.io](https://mihaylov.io),
and how to redeploy it afterwards with one command.

This is a **small deployment joining an existing stack**, which makes it unusual in a helpful way:
there is no DNS to configure, no TLS to terminate, no load balancer to stand up and no database to
provision. Traefik, Postgres, n8n and the portfolio's own two containers are already running on the
box and already share a Docker network. This project adds two more containers to it and takes no
host ports at all.

> **Two things that will bite, read first:**
>
> 1. **`vector` is not a trusted PostgreSQL extension.** Migration `0001` runs
>    `create extension if not exists vector`, and a non-superuser role **cannot** create it — it can
>    only skip it when it is already there. Verified both ways in §5. Check this *before* the first
>    deploy, because the failure comes in the middle of a migration and reads like a broken
>    migration rather than a missing privilege.
> 2. **This box is shared.** It runs the live site, the portfolio API, n8n and Postgres. Anything
>    with `-a` on it — `docker image prune -a`, `docker system prune -a` — reaches outside this
>    project. The deploy script's cleanup is label-scoped for exactly this reason (§9).

---

## Table of contents

1. [Configuration](#1-configuration)
2. [Architecture](#2-architecture)
3. [Order of operations](#3-order-of-operations)
4. [Prerequisites on the box](#4-prerequisites-on-the-box)
5. [Database preparation](#5-database-preparation)
6. [Files on the server](#6-files-on-the-server)
7. [GHCR access](#7-ghcr-access)
8. [First deploy, by hand](#8-first-deploy-by-hand)
9. [`deploy-portfolio-ai`](#9-deploy-portfolio-ai)
10. [`rollback-portfolio-ai`](#10-rollback-portfolio-ai)
11. [Operations](#11-operations)
12. [Cutover from n8n](#12-cutover-from-n8n)
13. [Gotcha index](#13-gotcha-index)

---

## 1. Configuration

| Decision | Choice | Affects |
|---|---|---|
| Host | **The existing EC2 instance**, alongside the site, n8n and Traefik | §4, §11 |
| Images | **Built in GitHub Actions → GHCR**, pulled on the box | §7, §8 |
| Services | **Two from one image** — `api` (FastAPI) and `worker` (supercronic) | §2 |
| Database | **The Postgres container already on the box**, reached by service name | §5 |
| Schema | **`portfolio_rag`**, owned entirely by this project | §5, §11 |
| Exposure | **None.** No published ports, no Traefik labels, internal only | §2 |
| Auth | Bearer token, checked by FastAPI, attached server-side by the Express API | §6, §12 |
| Migrations | **Alembic, run as a one-shot before the containers start** | §8, §9 |
| Timezone | **`TZ=Europe/London`** on the worker | §6, §11 |
| Deploy dir | **`/opt/portfolio-ai`** (override with `PORTFOLIO_AI_DIR`) | §6, §9 |

Everything environment-specific — the Postgres service name, credentials, the API key — lives in
`/opt/portfolio-ai/.env` on the box and nowhere else. **This repository is public**, so every value
of that kind appears here as a `<placeholder>`.

Two of those placeholders look interchangeable and are not. `<postgres-service>` is the **network
alias** other containers resolve, and it is what goes in `DATABASE_URL`. `<postgres-container>` is
the **container name**, and it is what `docker exec` takes. Compose sets both, and they are equal
only when the compose file says `container_name:` matching the service key — so check rather than
assume:

```bash
docker network inspect traefik_proxy \
  --format '{{range .Containers}}{{.Name}}{{"\n"}}{{end}}'
```

---

## 2. Architecture

```
                    Internet
                       |
                  [ Traefik ]  :443
                       |
        +--------------+--------------+
        |                             |
   my-portfolio                 my-portfolio-api        (already running)
   (nginx, static)              (Express, /api/*)
                                      |
                                      |  bearer token attached server-side
                                      |  http://portfolio-ai:8000
                                      v
                              +---------------+
                              |  portfolio-ai |   FastAPI, no host port,
                              |     :8000     |   no Traefik labels
                              +---------------+
                                      |
        +-----------------------------+----------------+
        |                                              |
  portfolio-ai-worker                            [ Postgres ]      (already running)
  supercronic: ingest / digest / purge            schema: portfolio_rag
        |                                              ^
        +----------------------------------------------+
                                      |
                              OpenAI API (egress)

                        all of the above on: traefik_proxy
```

- **`portfolio-ai`** — the chat API. **Deliberately has no Traefik labels and publishes no ports.**
  The only thing that talks to it is the Express API, from inside the network. Putting it on the
  internet and then guarding it with a bearer token would be two defences where one is needed, and
  the exposed one would be the weaker.
- **`portfolio-ai-worker`** — the same image, running `supercronic /etc/crontab` instead: daily
  ingestion, the weekly digest, the retention sweep.
- **Postgres** — already on the box and already shared with n8n. This project only ever writes to
  its own `portfolio_rag` schema. The live `mihaylov_rag_documents` and `mihaylov_chat_histories`
  tables belong to n8n and are not touched until cutover (§12).
- **One image, two services.** One build, one tag, one thing to roll back.

---

## 3. Order of operations

1. **Confirm the `vector` extension exists** on the production database and that the app role can
   create a schema (§5). Nothing else works until this is true, and it is the one step that can
   fail for a reason outside this repository.
2. **Create the database role and grant it `CREATE` on the database** (§5).
3. **Put `compose.prod.yaml` and `.env` on the box**, permissions set (§6).
4. **Make GHCR pullable** — either publish the package or log in with a PAT (§7).
5. **First deploy by hand**, one command at a time, so a failure names itself (§8).
6. **Install the helper scripts** (§9, §10).
7. **Watch the first scheduled ingestion run** the next morning (§11).
8. **Cutover** — only once the assistant and the front end are ready (§12).

Steps 1–7 can happen as soon as the ingestion pipeline exists. Step 8 is much later and is a
separate decision.

---

## 4. Prerequisites on the box

Everything here is already true — this section exists so that a rebuild from scratch does not
discover them one at a time.

- **Docker with Compose v2** (`docker compose`, not `docker-compose`).
- **The `traefik_proxy` network exists** and is external:
  ```bash
  docker network inspect traefik_proxy --format '{{.Name}} {{.Driver}}'
  ```
- **Postgres is running on that network**, reachable by service name. Nothing in this project
  requires it to publish a host port, and it should not.
- **Outbound HTTPS** to `api.openai.com`, `api.github.com`, `raw.githubusercontent.com`,
  `ghcr.io` and `smtp.gmail.com:587`.
- **Disk headroom.** Each pull is a fresh image of roughly 250 MB. §9 handles the accumulation;
  check `docker system df` before the first deploy so you know the starting point.

---

## 5. Database preparation

This is the section to do slowly. It is the only part of the deployment that can fail for reasons
that have nothing to do with this codebase.

### 5a. The `vector` extension must already exist

Migration `0001` opens with:

```sql
create extension if not exists vector with schema public;
```

`vector` is **not a trusted extension**, which in PostgreSQL means only a superuser can install it.
A normal role gets:

```
ERROR:  permission denied to create extension "vector"
HINT:  Must have CREATE privilege on current database and be a superuser...
```

`if not exists` does **not** rescue you — it only turns the statement into a no-op when the
extension is already present, which a non-superuser is allowed to do. Both halves were verified
against the development database, whose role is deliberately not a superuser:

```
role        : ('rag_assistant', False, False, False)   -- not superuser
vector      : trusted=False
create extension if not exists vector   -> OK (no-op)
create extension <absent, untrusted>    -> DENIED: permission denied to create extension
```

So the check before the first deploy is one query, run against the production database as **any**
role:

```sql
select extname, extversion from pg_extension where extname = 'vector';
```

- **A row comes back** → done. Nothing further is needed, and the migration's first statement will
  be a no-op. This is the expected answer, because n8n is already storing vectors in this database.
- **No row** → install it once, as a superuser, before deploying:
  ```sql
  create extension vector with schema public;
  ```
  Into `public`, not into `portfolio_rag`. Extensions are shared across a database, and putting it
  in our schema would mean every connection needed our schema on its search path merely to
  understand what a vector is.

> **Do not drop this extension to "reinstall it cleanly."** `drop extension vector` cascades to
> every column of type `vector` in the database — including n8n's live table. There is no situation
> in this deployment where dropping it is the right move.

### 5b. Version parity

```sql
select version();
select extversion from pg_extension where extname = 'vector';
```

Development runs **PostgreSQL 18.6 with pgvector 0.8.6**, and CI pins `pgvector/pgvector:0.8.6-pg18`
to match. If production is materially older, note it here — index behaviour is where pgvector
versions differ, and the HNSW index is the part the test suite exists to protect.

### 5c. The application role

The app does not need, and should not have, a superuser. It needs exactly two things: the ability
to create its own schema, and ownership of what it creates.

```sql
create role <db-user> login password '<generated>';
grant connect on database <db-name> to <db-user>;
grant create on database <db-name> to <db-user>;   -- so Alembic can create portfolio_rag
```

Verify, connected **as that role**:

```sql
select current_user,
       (select rolsuper from pg_roles where rolname = current_user) as is_superuser,
       has_database_privilege(current_user, current_database(), 'CREATE') as can_create_schema;
```

Wanted: `is_superuser = false`, `can_create_schema = true`.

> **Do not reuse n8n's database role.** A shared role means this project's credentials being
> rotated, leaked or revoked takes the live chat down with it, and it removes the only thing
> stopping a mistake here from writing to n8n's tables.

### 5d. Who creates the schema

Nobody, by hand. `migrations/env.py` creates `portfolio_rag` if it is missing, before Alembic looks
for its version table — the version table lives *inside* the schema, so something has to create it
first. Creating it manually is harmless but unnecessary.

---

## 6. Files on the server

```bash
sudo mkdir -p /opt/portfolio-ai
sudo chown "$USER:$USER" /opt/portfolio-ai
cd /opt/portfolio-ai
```

Two files, and only two. **There is no source checkout on the server** — the image carries the
code, which is the entire point of building it in CI.

```
/opt/portfolio-ai/
├── compose.prod.yaml     copied from this repo: docker/compose.prod.yaml
└── .env                  never committed, chmod 600
```

```bash
# Copy docker/compose.prod.yaml from the repository to the box, then:
chmod 600 .env
chmod 644 compose.prod.yaml
```

### `.env`

Every key here matches a field on `Settings` in `src/portfolio_ai/config.py`. Unrecognised keys are
**rejected at startup**, not ignored — a typo stops the container with a message naming it, rather
than quietly falling back to a default. Start from `.env.example` in the repository, which is
always current, and set these for production:

```bash
ENVIRONMENT=production

# Service name on traefik_proxy, not localhost and not an IP. Port 5432 here: the
# 5433 rule is a LOCAL-only guard, because locally 5432 is a different database
# without pgvector. On this box there is one Postgres and it is on 5432.
DATABASE_URL=postgresql://<db-user>:<password>@<postgres-service>:5432/<db-name>
DB_SCHEMA=portfolio_rag

OPENAI_API_KEY=<real key>

# Must be IDENTICAL to PORTFOLIO_AI_API_KEY in the Express API's own .env (§12).
# Generate with: openssl rand -hex 32
PORTFOLIO_AI_API_KEY=<generated>

TZ=Europe/London
LOG_LEVEL=INFO
CORS_ORIGINS=https://mihaylov.io
```

> **`ENVIRONMENT=production` matters more than it looks.** Two guards in this codebase key off it:
> the port validator in `config.py` (which would reject a 5432 URL if this said `local`), and the
> integration-test fixture, which refuses to create and drop schemas anywhere that is not `local`.
> Getting this wrong in the safe direction fails at boot. Getting it wrong in the other direction
> is why the second guard exists.

> **Never `COPY` this file into an image.** A Docker layer is immutable: a secret copied in and
> deleted later is still in the earlier layer, extractable by anyone who can pull the image. This
> one is public.

---

## 7. GHCR access

The first push from `main` creates the package `ghcr.io/mmihaylov94/portfolio-ai` and makes it
**private**, linked to the repository. Pick one:

**Public** (recommended). One setting, no credentials on the box, no token to rotate. The image
contains no secrets by construction — `.env` is in `.dockerignore` and configuration arrives as
environment variables at run time — and the source repository is public anyway, so the image
discloses nothing the repository does not.

> GitHub → the repository → *Packages* → `portfolio-ai` → *Package settings* → *Change visibility*.

**Private.** Then the box needs a classic PAT with `read:packages`, once:

```bash
echo '<pat>' | docker login ghcr.io -u <github-username> --password-stdin
```

That writes credentials to `~/.docker/config.json` in plain text, and the PAT expires — a deploy
that starts failing with `denied` months from now is this. If you choose private, note the expiry
date somewhere you will see it.

---

## 8. First deploy, by hand

Do the first one a command at a time. The script in §9 does exactly this, and running it manually
once means a failure names its own step instead of being one line in a wrapper's output.

```bash
cd /opt/portfolio-ai

# 1. Pull. Nothing has started yet, so a bad tag or a missing login fails here, harmlessly.
docker compose -f compose.prod.yaml pull

# 2. Check what the container thinks its configuration is, before it tries to use it.
#    Settings validates at import, so a bad .env fails here with a message naming the
#    variable -- which is a much better place to find out than mid-migration.
docker compose -f compose.prod.yaml run --rm --no-deps api \
    python -c "from portfolio_ai.config import get_settings; s=get_settings(); print(s.environment, s.db_schema)"

# 3. Migrate. Creates the portfolio_rag schema and eleven tables. Additive only --
#    it touches nothing outside its own schema.
docker compose -f compose.prod.yaml run --rm --no-deps api alembic upgrade head
docker compose -f compose.prod.yaml run --rm --no-deps api alembic current   # expect: <rev> (head)

# 4. Start both services.
docker compose -f compose.prod.yaml up -d
docker compose -f compose.prod.yaml ps

# 5. Watch them boot. JSON, one object per line.
docker compose -f compose.prod.yaml logs -f --tail=50
```

Confirm from the database side, as the app role:

```sql
\dn                            -- portfolio_rag present
\dt portfolio_rag.*            -- 11 tables + alembic_version
\di portfolio_rag.chunks*      -- an HNSW index on embedding
select version_num from portfolio_rag.alembic_version;
```

And that the Express API can reach it, from the Express container rather than the host — the host
is not on the network and a `curl` from there proves nothing:

```bash
docker exec my-portfolio-api wget -qO- http://portfolio-ai:8000/healthz
```

> **`/healthz` and `/readyz` do not exist yet.** They land with the API step. Until then the check
> is `docker compose ps` showing the container up and the logs showing no traceback. The deploy
> script treats a missing health endpoint as "skipped", not "failed" — see §9.

---

## 9. `deploy-portfolio-ai`

The routine redeploy: pull the new image, migrate, restart, verify, clean up. It lives in
`/usr/local/bin` on the box rather than in the repository, because it is box-local operations
tooling that needs to run from any directory.

```bash
deploy-portfolio-ai              # the normal case
deploy-portfolio-ai --no-backup  # skip the pre-migration dump
deploy-portfolio-ai --backup     # force one even with no migrations pending
```

In order it:

1. **Records the running image digests** to `.deploy-state/previous-images.txt`. Taken from the
   live containers rather than from the `:latest` tag, so the rollback target is what is actually
   deployed — which is not the same thing the moment `:latest` moves.
2. **Pulls.**
3. **Dumps `portfolio_rag` if, and only if, migrations are pending.** The check is read-only:
   `alembic current` prints `(head)` when the database is up to date, and does not otherwise.
4. **Migrates.** A no-op when nothing is pending.
5. **Restarts** with `up -d`.
6. **Probes readiness**, from inside the container, and treats a 404 as "not built yet".
7. **Prunes old images of this project only.**

`set -euo pipefail` is what makes step 3 meaningful: a failed dump aborts before step 4, so the
migration never runs without a completed backup.

> **Step 7 is label-scoped, and that is not fussiness.** The obvious cleanup —
> `docker image prune -af --filter until=168h` — removes every unused image on the host, and this
> host is shared with the live site, its API, n8n and Traefik. Pruning their previous images means
> *their* rollback target is gone too, decided by a script belonging to a different project. The
> filter below matches only images carrying this repository's
> `org.opencontainers.image.source` label, which `docker/metadata-action` stamps on every build in
> CI. Nothing else on the box can match it.

> **Database rollback is deliberately not automated.** `alembic downgrade` drops tables and
> columns, which is data loss dressed up as a command. Step 3 takes the dump; restoring it is a
> decision a person makes, in §11.

```bash
#!/usr/bin/env bash
# /usr/local/bin/deploy-portfolio-ai
#
# Pull the latest image, migrate, restart, verify, clean up.
# Overridable: PORTFOLIO_AI_DIR, PORTFOLIO_AI_PG_CONTAINER, PORTFOLIO_AI_PG_DB,
#              PORTFOLIO_AI_PG_USER, PORTFOLIO_AI_BACKUP_RETENTION_DAYS
set -euo pipefail

DEPLOY_DIR="${PORTFOLIO_AI_DIR:-/opt/portfolio-ai}"
COMPOSE_FILE="${PORTFOLIO_AI_COMPOSE:-compose.prod.yaml}"
SERVICES="${PORTFOLIO_AI_SERVICES:-api worker}"
IMAGE_LABEL="org.opencontainers.image.source=https://github.com/mmihaylov94/portfolio-ai"
STATE_DIR="$DEPLOY_DIR/.deploy-state"
BACKUP_DIR="${PORTFOLIO_AI_BACKUPS:-$DEPLOY_DIR/backups}"
RETAIN_DAYS="${PORTFOLIO_AI_BACKUP_RETENTION_DAYS:-30}"
IMAGE_RETAIN_HOURS="${PORTFOLIO_AI_IMAGE_RETENTION_HOURS:-168}"

# The Postgres container, and a role that may read the portfolio_rag schema. The dump
# runs INSIDE that container on purpose: pg_dump refuses to dump a server newer than
# itself, and the server's own binaries are by definition the right version.
PG_CONTAINER="${PORTFOLIO_AI_PG_CONTAINER:?set PORTFOLIO_AI_PG_CONTAINER}"
PG_DB="${PORTFOLIO_AI_PG_DB:?set PORTFOLIO_AI_PG_DB}"
PG_USER="${PORTFOLIO_AI_PG_USER:-postgres}"

BACKUP_MODE="auto"
case "${1:-}" in
  -n|--no-backup) BACKUP_MODE="never" ;;
  -b|--backup)    BACKUP_MODE="always" ;;
  "")             ;;
  *) echo "usage: $(basename "$0") [--backup|--no-backup]" >&2; exit 2 ;;
esac

cd "$DEPLOY_DIR"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- 1. Record the rollback target ------------------------------------------
# From the running containers, not from the tag: by the time we pull, :latest
# means something else, and this file has to say what was actually live.
say "Recording current images"
mkdir -p "$STATE_DIR"
: > "$STATE_DIR/previous-images.txt"
for svc in $SERVICES; do
  cid="$(compose ps -q "$svc" 2>/dev/null || true)"
  [ -n "$cid" ] || { echo "  $svc: not running, nothing to record"; continue; }
  image_id="$(docker inspect --format '{{.Image}}' "$cid")"
  # RepoDigests is the pullable ghcr.io/...@sha256:... form. A locally built image
  # has none, so fall back to the image id, which still works on this host.
  ref="$(docker image inspect \
          --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' \
          "$image_id")"
  echo "$svc $ref" >> "$STATE_DIR/previous-images.txt"
  echo "  $svc $ref"
done

# --- 2. Pull ----------------------------------------------------------------
say "Pulling"
compose pull

# --- 3. Back up, if migrations are pending ----------------------------------
# `alembic current` prints "<rev> (head)" when the database is up to date. grep for
# the marker rather than parsing: the container also writes structlog JSON to stdout,
# and a parser that assumes clean output breaks the first time a log line appears.
pending=1
if compose run --rm --no-deps api alembic current 2>/dev/null | grep -q '(head)'; then
  pending=0
fi

do_backup=0
case "$BACKUP_MODE" in
  always) do_backup=1 ;;
  never)  do_backup=0 ;;
  auto)   [ "$pending" -eq 1 ] && do_backup=1 ;;
esac

if [ "$do_backup" -eq 1 ]; then
  say "Migrations pending -- dumping portfolio_rag first"
  mkdir -p "$BACKUP_DIR"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  out="$BACKUP_DIR/portfolio_rag-$stamp.dump"
  # Custom format (-Fc), schema-scoped, no ownership or ACLs -- so it restores into
  # a database whose roles are named differently without a pile of errors.
  docker exec "$PG_CONTAINER" pg_dump \
      -U "$PG_USER" -d "$PG_DB" \
      --schema=portfolio_rag --no-owner --no-privileges -Fc > "$out"
  # An empty dump is a failed dump that exited 0 -- catch it here, not at restore.
  [ -s "$out" ] || { echo "dump is empty: $out" >&2; exit 1; }
  echo "  $out ($(du -h "$out" | cut -f1))"
  find "$BACKUP_DIR" -name 'portfolio_rag-*.dump' -mtime "+$RETAIN_DAYS" -print -delete
else
  say "No migrations pending -- skipping backup"
fi

# --- 4. Migrate -------------------------------------------------------------
say "Migrating"
compose run --rm --no-deps api alembic upgrade head

# --- 5. Restart -------------------------------------------------------------
say "Starting"
compose up -d
compose ps

# --- 6. Readiness -----------------------------------------------------------
# From inside the container, because this service publishes no host port and the
# host is not on traefik_proxy. Uses the image's own Python; no extra image to pull.
say "Readiness"
ready=0
for _ in $(seq 1 15); do
  if compose exec -T api python - <<'PROBE' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except urllib.error.HTTPError as e:
    # 404 means the API step has not landed yet. Not a failure of this deploy.
    sys.exit(0 if e.code == 404 else 1)
except Exception:
    sys.exit(1)
PROBE
  then ready=1; break; fi
  sleep 2
done
[ "$ready" -eq 1 ] && echo "  ok" || { echo "  NOT READY -- see: compose logs api" >&2; exit 1; }

# --- 7. Clean up this project's old images only -----------------------------
# Label-scoped. A bare `prune -a` on this box would also remove the previous images
# of the live site, its API, n8n and Traefik -- deleting someone else's rollback
# target. `prune` never removes an image a container is using, so the images
# recorded in step 1 survive as long as the containers do.
say "Pruning images older than ${IMAGE_RETAIN_HOURS}h"
docker image prune -af \
  --filter "until=${IMAGE_RETAIN_HOURS}h" \
  --filter "label=$IMAGE_LABEL"

say "Deployed"
```

### Install

```bash
# Quoted delimiter ('EOS'): without the quotes the shell expands $svc, $(date) and
# friends as it writes, and you get a file full of blanks that looks fine.
sudo tee /usr/local/bin/deploy-portfolio-ai >/dev/null <<'EOS'
…paste the script above…
EOS
sudo chmod 0755 /usr/local/bin/deploy-portfolio-ai

# The two values with no sensible default. Put them where a login shell will see them.
sudo tee /etc/profile.d/portfolio-ai.sh >/dev/null <<'EOF'
export PORTFOLIO_AI_PG_CONTAINER=<postgres-container>
export PORTFOLIO_AI_PG_DB=<db-name>
export PORTFOLIO_AI_PG_USER=postgres
EOF
. /etc/profile.d/portfolio-ai.sh

# Prove the dump path works before trusting it to abort a migration.
docker exec "$PORTFOLIO_AI_PG_CONTAINER" pg_dump --version
deploy-portfolio-ai --backup
```

That last line is the real test: it forces a dump even with nothing pending, so the backup path is
exercised on a day when it is not load-bearing.

---

## 10. `rollback-portfolio-ai`

**Image-only.** It reads `previous-images.txt`, pins both services to those digests through a
Compose override, and restarts. It never touches the database.

```bash
#!/usr/bin/env bash
# /usr/local/bin/rollback-portfolio-ai
set -euo pipefail

DEPLOY_DIR="${PORTFOLIO_AI_DIR:-/opt/portfolio-ai}"
COMPOSE_FILE="${PORTFOLIO_AI_COMPOSE:-compose.prod.yaml}"
STATE="$DEPLOY_DIR/.deploy-state/previous-images.txt"

cd "$DEPLOY_DIR"
[ -s "$STATE" ] || { echo "no recorded images at $STATE" >&2; exit 1; }

override="$DEPLOY_DIR/.deploy-state/rollback.yaml"
{
  echo "services:"
  while read -r svc ref; do
    [ -n "$svc" ] || continue
    printf '  %s:\n    image: %s\n' "$svc" "$ref"
  done < "$STATE"
} > "$override"

echo "Rolling back to:"
cat "$STATE"
cat <<'WARN'

This reverts the IMAGES ONLY. If the deploy you are undoing applied a migration,
the older image may not understand the current schema -- in which case the fix is
restoring the dump from /opt/portfolio-ai/backups, not this script.
WARN

read -r -p "Proceed? [y/N] " reply
[ "$reply" = "y" ] || { echo "aborted"; exit 1; }

docker compose -f "$COMPOSE_FILE" -f "$override" up -d
docker compose -f "$COMPOSE_FILE" -f "$override" ps
echo
echo "Pinned. The next deploy-portfolio-ai moves forward on :latest again --"
echo "fix the bad build rather than leaving this in place."
```

The override is not sticky by design: the next `deploy-portfolio-ai` runs without it and moves
forward. Rollback is a way to stop the bleeding, not a state to live in.

---

## 11. Operations

### Logs

Everything is JSON, one object per line, on stdout — collected by Docker.

```bash
cd /opt/portfolio-ai
docker compose -f compose.prod.yaml logs -f api
docker compose -f compose.prod.yaml logs --since 24h worker | grep -v '"level":"info"'
```

**Check the daemon's rotation policy before this box has been running for a year.** Without
`max-size`, a container's log file grows until the disk is full, and on a shared box that takes
the live site down with it:

```bash
docker inspect --format '{{.Name}} {{.HostConfig.LogConfig.Config}}' $(docker ps -q)
```

Blank config means uncapped. The fix belongs in `/etc/docker/daemon.json` and applies to
containers **created after** the change — an existing container keeps its old settings through a
daemon restart, so it looks done and is not. Recreate with `up -d --force-recreate`.

### The scheduled jobs

The worker runs supercronic, which logs each job's start and finish to stdout. The first thing to
check the morning after a deploy:

```bash
docker compose -f compose.prod.yaml logs --since 12h worker
```

An ingestion run that found nothing to do is the normal case — `content_hash` means an unchanged
document is not re-embedded, so the daily cost after the first run is close to zero.

**Verify `TZ` actually took effect**, because a wrong timezone is silent and shifts every job by an
hour for half the year:

```bash
docker compose -f compose.prod.yaml exec worker date
```

Manual runs, when you do not want to wait for the schedule:

```bash
docker compose -f compose.prod.yaml run --rm worker python -m portfolio_ai.ingestion --dry-run
docker compose -f compose.prod.yaml run --rm worker python -m portfolio_ai.ingestion --force
```

> **`--dry-run` first, every time, against production.** It plans and prints and writes nothing,
> and it is the only cheap way to find out that a change to chunking is about to re-embed the
> entire corpus.

### Backups

`deploy-portfolio-ai` dumps `portfolio_rag` before any migration and keeps 30 days of them in
`/opt/portfolio-ai/backups`. That covers the deploy-shaped risk. It does **not** cover the box
dying, so if the Postgres container is not already in a host-level backup, that is a separate
problem and a more important one.

Restore is deliberately manual:

```bash
# Into a scratch schema first, always. Never straight over the live one.
docker exec -i <postgres-container> pg_restore \
    -U postgres -d <db-name> --no-owner --no-privileges \
    --schema=portfolio_rag < /opt/portfolio-ai/backups/portfolio_rag-<stamp>.dump
```

> The dump contains columns of type `public.vector`. Restoring into a database without the
> extension fails on the first `CREATE TABLE` with `type "vector" does not exist` — which reads
> like a corrupt dump and is not. §5a again.

### Cost

Two things spend money, and neither is bounded by anything in Docker:

- **Ingestion** re-embeds only what changed, so a normal day is effectively free. A `--force` run
  re-embeds all eleven documents, which is fractions of a cent.
- **Chat** is the real number, and it scales with visitors. `DAILY_SPEND_CAP_USD` is the actual
  protection — on breach the API returns a polite refusal rather than an error. Set it before the
  chat is reachable from the site, not after.

### Rotating the API key

`PORTFOLIO_AI_API_KEY` appears in two `.env` files on this box: this project's and the Express
API's. They must match, and changing one without the other returns 401 to every visitor.

```bash
openssl rand -hex 32
# edit both .env files, then restart both:
docker compose -f /opt/portfolio-ai/compose.prod.yaml up -d api
docker compose -f <portfolio-dir>/docker-compose.yml up -d api
```

---

## 12. Cutover from n8n

Not part of the first deployment. Recorded here so the order is decided in advance rather than at
the moment of switching.

Until cutover, **both systems run side by side and neither notices the other.** n8n keeps serving
the live chat from `mihaylov_rag_documents`; this project writes only to `portfolio_rag`. That is
the whole point of the separate schema, and it means the vector store here can be built, ingested
daily and left to prove itself for weeks before anything is switched.

When the assistant, the API and the new chat UI are all ready:

1. **Ingest and verify** — `--force` once, then confirm the document and chunk counts match the
   eleven files in the knowledge base.
2. **Point the Express API at FastAPI.** `api/src/server.js` gains `/api/chat` and
   `/api/chat/feedback`, attaching the bearer key server-side, exactly as its contact handler
   already does. Watch for the streaming route: the response must be **piped, not buffered**, and
   compression disabled on it, or SSE arrives as a single chunk at the end.
3. **Deploy the new front end** — `@n8n/chat` removed entirely.
4. **Watch for a week.** Both systems still work; only the traffic has moved.
5. **Then, and only then**, disable the n8n workflows and drop `mihaylov_chat_histories`.
   `mihaylov_rag_documents` can go once the new ingestion has a month of clean runs behind it.

There is also a correction owed: `knowledgebase/projects/portfolio-ai-assistant.md` currently tells
visitors the assistant has thumbs up/down feedback and reCAPTCHA-protected chat. Neither is true
today. It becomes true at cutover, and the article should be fixed **in the same change**, because
until then the assistant is describing itself inaccurately to the people asking about it.

---

## 13. Gotcha index

- **`vector` is an untrusted extension** — a non-superuser cannot create it, only skip it when it
  already exists. Check with `select extname from pg_extension where extname='vector'` before the
  first deploy. §5a.
- **Never `drop extension vector`** to reinstall it — it cascades into n8n's live table.
- **`ENVIRONMENT=production`, not `local`.** Two guards key off it; `local` would reject a
  port-5432 URL and would let the test fixture believe it may drop schemas here.
- **Port 5432 in production, 5433 locally.** The 5433 rule is a local-only guard for a local-only
  hazard — two databases on one host, only one with pgvector.
- **`DATABASE_URL` uses the Postgres service name**, not `localhost` and not an IP. The container
  is not the host.
- **Unrecognised `.env` keys stop the container.** `extra="forbid"` is deliberate; a typo fails
  loudly at boot instead of silently using a default.
- **Never `COPY .env` into the image.** A layer is immutable, the image is public, and deleting
  the file later does not remove it from history.
- **`docker image prune -a` on this box reaches other projects.** The deploy script's prune is
  label-scoped for that reason.
- **The rollback target is read from the running containers**, not the `:latest` tag — by the time
  you have pulled, the tag means something else.
- **`alembic downgrade` is not a rollback**, it is data loss with a friendly name. Restore the
  dump instead.
- **A dump that exited 0 can still be empty.** The script checks, because a backup you discover is
  empty at restore time was never a backup.
- **`pg_dump` must not be older than the server.** Running it inside the Postgres container makes
  that impossible to get wrong.
- **Restoring a `portfolio_rag` dump needs the `vector` extension present first**, or it fails on
  the first table with `type "vector" does not exist`.
- **`TZ=Europe/London` on the worker.** Without it every crontab time is an hour out for half the
  year, and nothing reports it.
- **`PORTFOLIO_AI_API_KEY` lives in two `.env` files.** Rotate both, restart both.
- **`--dry-run` before any production ingestion.**
- **`DAILY_SPEND_CAP_USD` before the chat is reachable**, not after.
- **No Traefik labels on `portfolio-ai`** — it is internal, and exposing it would make the bearer
  token the only thing between the internet and an OpenAI bill.
- **Docker log rotation applies at container creation.** Changing `daemon.json` and restarting the
  daemon leaves existing containers uncapped while looking done.
- **A private GHCR package needs a PAT that expires.** A deploy failing with `denied` long after
  everything worked is usually that.
