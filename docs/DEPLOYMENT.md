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
| Database | **Its own database, `portfolio_ai`**, on the Postgres already on the box | §5 |
| Schema | **`portfolio_rag`**, owned entirely by this project | §5, §11 |
| Exposure | **None.** No published ports, no routing labels (`traefik.enable=false`), internal only | §2 |
| Auth | Bearer token, checked by FastAPI, attached server-side by the Express API | §6, §12 |
| Migrations | **Alembic, run as a one-shot before the containers start** | §8, §9 |
| Timezone | **`TZ=Europe/London`** on the worker | §6, §11 |
| Deploy dir | **`/opt/portfolio-ai`**, holding `docker-compose.yml` and `.env` | §6, §9 |

Everything environment-specific — the Postgres service name, credentials, the API key — lives in
`/opt/portfolio-ai/.env` on the box and nowhere else. **This repository is public**, so every value
of that kind appears here as a `<placeholder>`.

One placeholder does double duty. `<postgres-container>` is the Postgres container's **name**,
which is what `docker exec` takes — and on a user-defined network like `traefik_proxy`, a
container's name also resolves as a hostname for every other container on it, so the same name is
the host part of `DATABASE_URL`. §5a finds it and §5g proves it resolves.

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
                              |     :8000     |   no routing labels
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

- **`portfolio-ai`** — the chat API. **Deliberately has no routing labels and publishes no
  ports**; its one label, `traefik.enable=false`, keeps it off Traefik whatever Traefik's defaults
  are. The only thing that talks to it is the Express API, from inside the network. Putting it on
  the internet and then guarding it with a bearer token would be two defences where one is needed,
  and the exposed one would be the weaker. [API.md](API.md) walks through the code and the
  contract.
- **`portfolio-ai-worker`** — the same image, running `supercronic /etc/crontab` instead: hourly
  ingestion and the nightly retention sweep, and the weekly digest once step 7 builds it. Hourly sounds excessive and is not: a run
  with nothing to do costs one request to the GitHub tree API and about a second, because every
  file's blob SHA matches what is stored. What the frequency buys is a bound on staleness and,
  more importantly, a heartbeat — a webhook that stops firing looks exactly like a repository
  nobody has committed to, while a scheduled run that stops succeeding says so in the logs.
- **Postgres** — already on the box and already shared with n8n. This project only ever writes to
  its own `portfolio_rag` schema. The live `mihaylov_rag_documents` and `mihaylov_chat_histories`
  tables belong to n8n and are not touched until cutover (§12).
- **One image, two services.** One build, one tag, one thing to roll back.

---

## 3. Order of operations

1. **Create the role and a database it owns**, `portfolio_ai`, on the existing Postgres (§5).
2. **Install `vector` into that database as a superuser**, and prove the role can use it (§5).
   Nothing else works until this is true, and it is the one step that can fail for a reason
   outside this repository.
3. **Put `docker-compose.yml` and `.env` on the box**, permissions set (§6).
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

The end state is a **new database, `portfolio_ai`, owned by a new role**, on the Postgres
instance that already runs n8n — with the `vector` extension installed in it, and a proof, run as
that role over the network, that migration `0001` will succeed. Everything below is in order; each
step depends on the one before.

**Why a database of its own rather than a schema inside n8n's database.** A schema is a
naming convention; a database is a boundary. Postgres cannot query across databases, so no bug and
no wrong `search_path` in this project can reach n8n's tables, and `drop extension vector` here
cannot cascade into n8n's live vector column. It also makes `pg_dump portfolio_ai` the whole of
this project. The one cost: extensions are per database, so `vector` has to be installed into the
new one, by a superuser — which is 5f.

### 5a. Find the Postgres container

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep -iE 'postgres|pgvector'
```

Note the **name** (first column) and the **image** (second). The image should be
`pgvector/pgvector:…` or similar. If it is plain `postgres:…`, the pgvector files may not be
installed at all, and no amount of privilege will create the extension — see 5f.

On a user-defined network like `traefik_proxy`, a container's name resolves as a hostname for
every other container on it. So this one name is both what `docker exec` takes below and the host
part of `DATABASE_URL` in §6.

### 5b. Open a superuser session, and confirm pgvector is on the server

```bash
# The superuser and the default database the container was created with.
# Deliberately not `env`, which would print POSTGRES_PASSWORD to the terminal.
docker exec <postgres-container> printenv POSTGRES_USER POSTGRES_DB

docker exec -it <postgres-container> psql -U <POSTGRES_USER> -d <POSTGRES_DB>
```

No password prompt is normal: inside the container, over the local socket, the official image
trusts connections. That same trust is why 5g cannot be tested this way.

n8n's database is the place that proves the pgvector files are installed on this server:

```sql
\c <n8n-database>
\dx                             -- `vector` should be listed, with its version
select version();               -- the server's major version
```

If `vector` is listed, the extension files exist and 5f will work. Development runs PostgreSQL 18.6
with pgvector 0.8.6, and CI pins `pgvector/pgvector:0.8.6-pg18`; if production is materially older,
record it, because index behaviour is where pgvector versions differ.

### 5c. Generate a password

In a second terminal on the host:

```bash
openssl rand -hex 24
```

**Hex, deliberately.** The password ends up inside `DATABASE_URL`, which is a URL, and `@`, `:`,
`/`, `%` or `#` in it break the parsing. The error that produces names the host, not the password,
so the first hour goes on DNS.

### 5d. Create the role

Back in `psql`, as the superuser:

```sql
create role portfolio_ai login;
\password portfolio_ai
```

`\password` prompts for the password and sends it already hashed. Putting it in
`create role ... password '...'` instead would write it to the psql history file inside the
container and, if statement logging is on, to the server log.

**Grant it nothing else** — not superuser, not `createdb`, not `createrole`.

> **Do not reuse n8n's database role.** A shared role means this project's credentials being
> rotated, leaked or revoked takes the live chat down with it, and it removes the only thing
> stopping a mistake here from writing to n8n's tables.

### 5e. Create the database, owned by that role

```sql
create database portfolio_ai
    owner portfolio_ai
    template template0
    encoding 'UTF8'
    locale_provider builtin
    builtin_locale 'C.UTF-8';

revoke connect on database portfolio_ai from public;
```

**`template template0` and the builtin locale are not decoration** — the obvious
`create database portfolio_ai owner portfolio_ai` failed on this server:

```
ERROR:  template database "template1" has a collation version mismatch
DETAIL:  The template database was created using collation version 2.41, but the
         operating system provides version 2.36.
```

The Postgres data directory was initialised under glibc 2.41 (Debian 13) and the container now runs
on glibc 2.36 (Debian 12). Text sorting rules come from glibc, so Postgres refuses to copy a
template whose recorded rules no longer match the running ones. That is a property of the server,
not of this project — see *Collation version mismatch* in §11.

`template0` records no collation version at all — it is stipulated to contain nothing
collation-dependent — so the check does not apply to it, and nothing shared (`template1`) is
modified to get past it. `locale_provider builtin` then makes the new database's text rules
Postgres's own rather than glibc's, so a future image change cannot put *this* database into the
same state. Nothing in this project sorts human-language text in SQL: `doc_id` lookups are
equality and chunk order is an integer, so byte-order `C.UTF-8` is both correct and faster. It
needs PostgreSQL 17 or later; on 15 or 16, use `locale 'C'` in place of the last two lines.

**Owned by the app role, deliberately.** Since PostgreSQL 15 the `public` schema in a new database
belongs to `pg_database_owner` — whoever owns the database — so ownership gives the role everything
it needs inside it: creating the `portfolio_rag` schema, and using the `vector` type that will live
in `public`. No separate grants, and none of the PostgreSQL 15 "`CREATE` on `public` was revoked"
surprises.

**The revoke** is what makes the separate database actually separate. By default every role on the
server may connect to every new database; this limits `portfolio_ai` to its owner and superusers.
(n8n's database has the same default in the other direction. Connecting grants nothing by itself —
reading n8n's tables would still need privileges on them — so tightening that is optional, and it
is a change to n8n's database rather than this project's. Leave it unless you have a reason.)

Nobody creates the `portfolio_rag` schema by hand. `migrations/env.py` does, on the first
`alembic upgrade`.

### 5f. Install `vector` into the new database

Still the superuser:

```sql
\c portfolio_ai
create extension vector with schema public;
\dx                             -- vector, with the same version as in 5b
\q
```

Into `public`, not `portfolio_rag`: putting an extension in our schema would mean every connection
needed our schema on its search path merely to understand what a vector is.

This has to be the superuser because `vector` is **not a trusted extension** — only a superuser can
install it, and the app role never will be one. Migration `0001` opens with
`create extension if not exists vector with schema public`, which a normal role can run *only* as a
no-op, when the extension already exists. Verified against the development database, whose role is
deliberately not a superuser:

```
create extension if not exists vector   -> OK (no-op)
create extension <absent, untrusted>    -> DENIED: permission denied to create extension
```

So if this step is skipped, the very first migration fails with
`permission denied to create extension "vector"` — which reads like a broken migration.

- **`could not open extension control file`** → the Postgres image has no pgvector in it. That
  contradicts 5b, so check you are on the right container. Either way, stop: it is a change to the
  Postgres container, not to this project.

> **Never drop this extension to "reinstall it cleanly."** It cascades to every `vector` column in
> the database. In `portfolio_ai` that is only our own embeddings — re-creatable, but a full
> re-embed — and keeping that mistake away from n8n's table is one of the reasons for a separate
> database.

### 5g. Prove it, as the new role, over the network

This is the step that matters, and it has to be done **from another container on
`traefik_proxy`** — not with `docker exec` into the Postgres container. The official image trusts
loopback connections, so a test run from inside it never checks the password at all and passes
with a wrong one. A separate container connects the way the app will: by name, across the network,
with a password.

The client can be the Postgres container's own image — it is already on the box, so there is
nothing to pull:

```bash
docker run --rm --network traefik_proxy <postgres-image> \
  psql "postgresql://portfolio_ai:<password>@<postgres-container>:5432/portfolio_ai" -c "
    select current_user,
           current_database(),
           (select rolsuper from pg_roles where rolname = current_user) as superuser,
           has_database_privilege(current_database(), 'CREATE') as can_create_schema,
           has_schema_privilege('public', 'USAGE') as can_use_public;
    create extension if not exists vector with schema public;
  "
```

Expected:

```
 current_user | current_database | superuser | can_create_schema | can_use_public
--------------+------------------+-----------+-------------------+----------------
 portfolio_ai | portfolio_ai  | f         | t                 | t
NOTICE:  extension "vector" already exists, skipping
```

That last line is migration `0001`'s first statement, run as the real role against the real
database. If it prints the notice, the migration will get past it.

- **`password authentication failed`** → the password in the URL is not the one set by
  `\password`. Set it again.
- **`permission denied for database`** → the revoke in 5e ran but the ownership did not take:
  `\l portfolio_ai` should show `portfolio_ai` as owner.
- **`permission denied to create extension`** → 5f was skipped, or ran in a different database.
- **`could not translate host name`** → the container name is wrong, or it is not on
  `traefik_proxy`: `docker network inspect traefik_proxy`.

### 5h. Keep this for §6

```bash
DATABASE_URL=postgresql://portfolio_ai:<password>@<postgres-container>:5432/portfolio_ai
```

The deploy script in §9 needs nothing from here: its configuration is a block at the top of the
script itself.

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
├── docker-compose.yml    copied from this repo: docker/docker-compose.yml
└── .env                  never committed, chmod 600
```

```bash
# Copy docker/docker-compose.yml from the repository to the box, then:
chmod 600 .env
chmod 644 docker-compose.yml
```

### `.env`

Every key here matches a field on `Settings` in `src/portfolio_ai/config.py`, apart from `TZ`, which
is for the container rather than the app. Start from `.env.example` in the repository, which is
always current, and set these for production:

```bash
ENVIRONMENT=production

# Service name on traefik_proxy, not localhost and not an IP. Port 5432 here: the
# 5433 rule is a LOCAL-only guard, because locally 5432 is a different database
# without pgvector. On this box there is one Postgres and it is on 5432.
DATABASE_URL=postgresql://portfolio_ai:<password>@<postgres-container>:5432/portfolio_ai
DB_SCHEMA=portfolio_rag

OPENAI_API_KEY=<real key>

# Must be IDENTICAL to PORTFOLIO_AI_API_KEY in the Express API's own .env (§12).
# At least 32 characters; the API will not start without it.
# Generate with: openssl rand -hex 32
PORTFOLIO_AI_API_KEY=<generated>

# The key visitors' IP addresses are hashed with before they are stored. Not shared
# with anything. At least 32 characters; the API will not start without it.
# Generate with: openssl rand -hex 32
IP_HASH_SALT=<generated>

# The most visitors can spend in any 24 hours, in dollars. Past it, every message
# gets a polite "back tomorrow" instead of an answer. 1.00 is about 250 answers.
# 0 turns the chat off. See "The daily spending limit" in §11.
DAILY_SPEND_CAP_USD=1.00

# Source of the knowledge base. The defaults are already correct for this
# deployment, so these are here to be findable rather than because they need
# setting. GITHUB_TOKEN is optional while the repository is public -- a run makes
# one API call against a limit of sixty an hour -- and becomes required if it
# ever goes private.
GITHUB_REPO=mmihaylov94/my-portfolio
GITHUB_BRANCH=main
GITHUB_DOCS_PATH=knowledgebase
# GITHUB_TOKEN=

TZ=Europe/London
LOG_LEVEL=INFO
```

> **A misspelt key is ignored here, not rejected.** Locally, `Settings` reads `.env` itself and
> refuses a key it does not know (`extra="forbid"`). In the containers it never sees the file:
> Compose hands its lines over as environment variables, and settings are read from the
> environment by name -- a variable that matches no setting is simply not looked at. So on this
> box `DAILY_SPEND_CAP_UDS=5` would be ignored and the cap would stay at its default. Check what
> the containers actually see with the command in §8 step 2, after any edit.

Everything else in `.env.example` has a working default and can be left out. A few are worth
knowing about before you need them:

- **`INGESTION_MAX_PURGE_FRACTION`** (default `0.3`) caps how much of the knowledge base one run
  may delete, as a share of what is stored. It is what stops a bad minute at GitHub wiping the
  index. If you genuinely delete several articles at once the run will abort — raise it for that
  one run rather than lowering the guard permanently.
- **`EMBEDDING_MODEL`** and **`EMBEDDING_DIMENSIONS`** are recorded, not tunable. Changing either
  invalidates every stored vector and needs a full re-embed and a migration.
- **`SESSION_RATE_LIMIT`** (20 per `SESSION_RATE_WINDOW_MINUTES`, 15) and **`MAX_CONCURRENT_TURNS`**
  (10) are the API's other limits. The per-IP limit is the Express API's.
- **`CHAT_RETENTION_DAYS`** (90) is how long chat is kept; the nightly purge enforces it.

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
docker compose pull

# 2. Check what the container thinks its configuration is, before it tries to use it.
#    A value that does not validate fails here with a message naming the variable --
#    a much better place to find out than mid-migration. A misspelt key does not
#    fail at all (see the note in §6), which is why this prints what was read.
docker compose run --rm --no-deps worker python -c "
from portfolio_ai.config import get_settings
s = get_settings()
print('environment   ', s.environment, s.db_schema)
print('API secrets   ', 'key set' if s.portfolio_ai_api_key else 'KEY MISSING', '/', 'salt set' if s.ip_hash_salt else 'SALT MISSING')
print('limits        ', s.session_rate_limit, 'per', s.session_rate_window_minutes, 'min;', s.max_concurrent_turns, 'in flight')
print('spending cap  ', '$' + str(s.daily_spend_cap_usd), 'a day')
print('retention     ', s.chat_retention_days, 'days')
"

# 3. Migrate. Creates the portfolio_rag schema and eleven tables. Additive only --
#    it touches nothing outside its own schema.
docker compose run --rm --no-deps worker alembic upgrade head
docker compose run --rm --no-deps worker alembic current   # expect: <rev> (head)

# 4. Start both services.
docker compose up -d
docker compose ps

# 5. Watch them boot. JSON, one object per line.
docker compose logs -f --tail=50

# 6. The first ingestion, by hand, dry first. --dry-run plans and prints and
#    writes nothing, so a wrong GITHUB_REPO or an unreachable database fails
#    here rather than halfway through a real run.
docker compose run --rm worker \
    python -m portfolio_ai.ingestion --dry-run

# Expect: discovered 11, indexed 11, cost about $0.0003. Then for real:
docker compose run --rm worker \
    python -m portfolio_ai.ingestion
```

Confirm from the database side, as the app role:

```sql
\dn                            -- portfolio_rag present
\dt portfolio_rag.*            -- 11 tables + alembic_version
\di portfolio_rag.chunks*      -- an HNSW index on embedding
select version_num from portfolio_rag.alembic_version;

-- After step 6, the knowledge base itself:
select count(*) from portfolio_rag.documents;   -- 11
select count(*) from portfolio_rag.chunks;      -- 111
```

And that the Express API can reach it, from the Express container rather than the host — the host
is not on the network and a `curl` from there proves nothing:

```bash
docker exec my-portfolio-api wget -qO- http://portfolio-ai:8000/healthz   # {"status":"ok"}
```

Then the key and the database, from another container on the network, the way the Express API
will call it. Only reads and a 404, so nothing is written: a real question here would be stored
among visitors' conversations, which is why the answer path is checked with the terminal chat's
`--no-save` instead (§11).

```bash
docker compose run --rm --no-deps -T worker python - <<'EOF'
import json, os, urllib.error, urllib.request

def status(path, body=None, key=None):
    request = urllib.request.Request(
        "http://portfolio-ai:8000" + path,
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json"},
    )
    if key:
        request.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code

vote = {"session_id": "deploy-check-0000", "rating": 1}
key = os.environ["PORTFOLIO_AI_API_KEY"]
print("readyz                     ", status("/readyz"), "(expect 200)")
print("feedback without the key   ", status("/v1/messages/1/feedback", vote), "(expect 401)")
print("feedback with the key      ", status("/v1/messages/1/feedback", vote, key), "(expect 404)")
EOF
```

The 404 is the point of the third line: it means the key was accepted and the database was
queried, and no answer 1 belongs to a conversation called `deploy-check-0000`.

And that nothing outside can reach it:

```bash
# From anywhere: the site answers this path, not the API. Expect 405 from the site's
# nginx -- the API would have said 401.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://mihaylov.io/v1/chat

# On the host: nothing listens on 8000.
curl -s -m 3 http://localhost:8000/healthz || echo "nothing on port 8000, as intended"
```

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
6. **Verifies**: the worker is running, and the API is healthy (Docker's own check) and ready (it
   can reach the database). How soon Docker runs its first check depends on the engine: within
   seconds on current releases, only after the 30-second `interval` on older ones. So this step
   waits up to a minute.
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
# Pull the latest image, back up if migrations are pending, migrate, restart,
# verify, clean up. Usage:
#
#   deploy-portfolio-ai              normal deploy
#   deploy-portfolio-ai --backup     force a backup even with nothing to migrate
#   deploy-portfolio-ai --no-backup  skip the backup
set -euo pipefail

# --- Configuration -----------------------------------------------------------
DEPLOY_DIR="/opt/portfolio-ai"
COMPOSE_FILE="docker-compose.yml"

PG_CONTAINER="postgres"        # the Postgres container
PG_DB="portfolio_ai"           # this project's database
PG_USER="portfolio_ai"         # owns that database, so it can dump it
PG_SCHEMA="portfolio_rag"      # what gets backed up

BACKUP_DIR="$DEPLOY_DIR/backups"
BACKUP_RETENTION_DAYS=30
IMAGE_RETENTION_HOURS=168      # 7 days
IMAGE_LABEL="org.opencontainers.image.source=https://github.com/mmihaylov94/portfolio-ai"
# -----------------------------------------------------------------------------

BACKUP_MODE="auto"
case "${1:-}" in
  -b|--backup)    BACKUP_MODE="always" ;;
  -n|--no-backup) BACKUP_MODE="never" ;;
  "")             ;;
  *) echo "usage: $(basename "$0") [--backup|--no-backup]" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

cd "$DEPLOY_DIR" || die "$DEPLOY_DIR does not exist"
[ -f "$COMPOSE_FILE" ] || die "$DEPLOY_DIR/$COMPOSE_FILE not found"
[ -f .env ] || die "$DEPLOY_DIR/.env not found"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# One-off commands run in a throwaway container from the worker's definition:
# same image, same .env, same network. The worker rather than the api, because the
# scheduled jobs run in it, so a command that works here works for them.
oneshot() { compose run --rm --no-deps -T worker "$@"; }

# --- 1. Record what is running now, as the rollback target -------------------
say "Recording current images"
mkdir -p .deploy-state
: > .deploy-state/previous-images.txt
for svc in api worker; do
  cid="$(compose ps -q "$svc" 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    echo "  $svc: not running"
    continue
  fi
  image_id="$(docker inspect --format '{{.Image}}' "$cid")"
  ref="$(docker image inspect \
          --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' \
          "$image_id")"
  echo "$svc $ref" >> .deploy-state/previous-images.txt
  echo "  $svc: $ref"
done

# --- 2. Pull ------------------------------------------------------------------
say "Pulling"
compose pull

# --- 3. Back up, if there is anything to migrate -----------------------------
# `alembic current` prints "<revision> (head)" when the database is up to date.
# Captured whole so that a failure here -- a wrong DATABASE_URL, an unreachable
# database -- stops the deploy with the real error instead of carrying on.
say "Checking migration state"
if ! current="$(oneshot alembic current 2>&1)"; then
  echo "$current" >&2
  die "could not read the migration state -- the real error is printed just above"
fi
if grep -q '(head)' <<<"$current"; then
  pending=0
  echo "  up to date"
else
  pending=1
  echo "  migrations pending"
fi

do_backup=0
case "$BACKUP_MODE" in
  always) do_backup=1 ;;
  auto)   do_backup=$pending ;;
esac

if [ "$do_backup" -eq 1 ]; then
  schema_exists="$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc \
    "select 1 from pg_namespace where nspname = '$PG_SCHEMA'")"

  if [ "$schema_exists" != "1" ]; then
    # First deploy: nothing exists yet, so there is nothing to lose.
    say "Backup skipped -- $PG_SCHEMA does not exist yet"
  else
    say "Backing up $PG_SCHEMA"
    mkdir -p "$BACKUP_DIR"
    out="$BACKUP_DIR/$PG_SCHEMA-$(date -u +%Y%m%dT%H%M%SZ).dump"

    # Run inside the Postgres container, so pg_dump is always the server's own
    # version. No -t on docker exec: a TTY would corrupt the binary dump.
    docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
      --schema="$PG_SCHEMA" --no-owner --no-privileges -Fc > "$out"

    # An empty file is a failed dump that exited 0. Refuse to migrate on it.
    [ -s "$out" ] || die "backup is empty: $out -- not migrating"
    echo "  $out ($(du -h "$out" | cut -f1))"

    find "$BACKUP_DIR" -name "$PG_SCHEMA-*.dump" -mtime "+$BACKUP_RETENTION_DAYS" -print -delete
  fi
fi

# --- 4. Migrate ---------------------------------------------------------------
say "Migrating"
oneshot alembic upgrade head

# --- 5. Restart ---------------------------------------------------------------
say "Starting"
compose up -d --remove-orphans

# --- 6. Verify ----------------------------------------------------------------
say "Verifying"

# The worker has to be up and staying up. A few seconds' grace, because a
# container that crashes on start is briefly "running" before it is not.
sleep 5
worker_cid="$(compose ps -q worker 2>/dev/null || true)"
[ -n "$worker_cid" ] || die "worker container was not created -- see: docker compose -f $COMPOSE_FILE logs worker"
worker_state="$(docker inspect --format '{{.State.Status}}' "$worker_cid")"
[ "$worker_state" = "running" ] || die "worker is '$worker_state' -- see: docker compose -f $COMPOSE_FILE logs worker"
echo "  worker: running"

# The API has to become healthy -- Docker's own check against /healthz -- and then
# ready, which needs the database. The first check comes within seconds on a current
# Docker engine and after the 30 s interval on an older one, so this waits up to a
# minute before giving up.
api_cid="$(compose ps -q api 2>/dev/null || true)"
[ -n "$api_cid" ] || die "api container was not created -- see: docker compose -f $COMPOSE_FILE logs api"
api_health="starting"
for _ in $(seq 1 30); do
  api_health="$(docker inspect --format '{{.State.Health.Status}}' "$api_cid")"
  [ "$api_health" = "starting" ] || break
  sleep 2
done
[ "$api_health" = "healthy" ] || die "api is '$api_health' -- see: docker compose -f $COMPOSE_FILE logs api"
compose exec -T api python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5)" \
  || die "api is up but not ready -- usually the database; see: docker compose -f $COMPOSE_FILE logs api"
echo "  api: healthy and ready"

# The real check: exactly what the worker does every hour, minus the writing.
# Proves the image runs, .env is valid, GitHub and the database are reachable,
# and the schema is in place. Reads only; spends nothing.
echo
oneshot python -m portfolio_ai.ingestion --dry-run \
  || die "ingestion dry run failed -- the hourly job would fail the same way"

# --- 7. Clean up this project's old images only ------------------------------
# Label-scoped on purpose: this server also runs the site, n8n and Postgres, and
# a bare `prune -a` would delete their previous images too.
say "Pruning this project's images older than ${IMAGE_RETENTION_HOURS}h"
docker image prune -af \
  --filter "until=${IMAGE_RETENTION_HOURS}h" \
  --filter "label=$IMAGE_LABEL"

say "Deployed"
```

### Install

```bash
sudo nano /usr/local/bin/deploy-portfolio-ai     # paste the script above, save
sudo chmod 0755 /usr/local/bin/deploy-portfolio-ai

deploy-portfolio-ai --backup
```

The configuration is the block at the top of the script, with this deployment's real values:
the Postgres container is `postgres`, the database and its owning role are both `portfolio_ai`.
An earlier version read those from environment variables set in `/etc/profile.d/`, which only
login shells source — under `sudo` or a fresh terminal they were simply absent and the script
died on its first lines. If you created that file, it is no longer used and can be deleted.

`--backup` on the first run forces a dump with nothing to migrate, so the backup path is
proved on a day when it is not load-bearing.

The dump runs as `portfolio_ai`, which owns the database and so can read all of it, through
`docker exec` into the Postgres container. That relies on the official image trusting
connections over the local socket, which is also what let you open `psql` without a password
in §5b.

### Releases that change the box's own files

Most releases need nothing but `deploy-portfolio-ai`: the image carries the code, and the script
pulls it. A release that changes `docker-compose.yml`, `.env` or one of these scripts needs those
changed on the box first, because nothing copies them there.

The API release (build step 4) is one. On a box that already runs ingestion:

1. **`.env`.** Add `IP_HASH_SALT`, generated with `openssl rand -hex 32`, and
   `DAILY_SPEND_CAP_USD=1.00`. Check that `PORTFOLIO_AI_API_KEY` is set and at least 32 characters
   long, since the API will not start otherwise. Delete `CORS_ORIGINS` if it is there. The setting
   is gone, and the containers ignore the line rather than fail on it (§6), so nothing will remind
   you.
2. **`docker-compose.yml`.** Copy the repository's `docker/docker-compose.yml` over the one on the
   box. It enables the `api` service, with its health check and its 45 s stop grace period, and
   puts `traefik.enable=false` on both services.
3. **The scripts.** Replace `/usr/local/bin/deploy-portfolio-ai` with the script above, and
   `/usr/local/bin/rollback-portfolio-ai` with the one in §10. Both now wait for the API to be
   healthy and ready. The deploy script from before this release does not know the API exists.
4. **Check what the containers will read**, with §8 step 2. A misspelt key shows up there as a
   setting still at its default.
5. **Deploy** with `deploy-portfolio-ai`. This release has no migration, so no backup is taken.
6. **Check it from the network**, with the calls at the end of §8, none of which writes anything:
   `/healthz` from the Express container, then readiness, a 401 and a feedback 404, then that nothing
   outside can reach it.
7. **The next morning**, confirm the retention sweep ran: `docker compose logs --since 12h worker`
   shows a `retention_purged` line. On a new deployment it deletes nothing, because no chat is 90
   days old yet.

Then set a monthly budget on the OpenAI project, if there is not one already (§11, Cost).

---

## 10. `rollback-portfolio-ai`

**Image-only.** Puts the containers back on the images that were running before the last
`deploy-portfolio-ai`, which records them before it pulls anything. It never touches the
database.

```bash
rollback-portfolio-ai          # shows what it will pin, asks first
rollback-portfolio-ai --yes    # does not ask
```

```bash
#!/usr/bin/env bash
# /usr/local/bin/rollback-portfolio-ai
#
# Put the containers back on the images that were running before the last
# deploy-portfolio-ai. Images only -- the database is never touched. Usage:
#
#   rollback-portfolio-ai        asks before changing anything
#   rollback-portfolio-ai --yes  does not ask
set -euo pipefail

# --- Configuration -----------------------------------------------------------
DEPLOY_DIR="/opt/portfolio-ai"
COMPOSE_FILE="docker-compose.yml"
# -----------------------------------------------------------------------------

STATE=".deploy-state/previous-images.txt"   # written by deploy-portfolio-ai
OVERRIDE=".deploy-state/rollback.yaml"

ASSUME_YES=0
case "${1:-}" in
  -y|--yes) ASSUME_YES=1 ;;
  "")       ;;
  *) echo "usage: $(basename "$0") [--yes]" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

cd "$DEPLOY_DIR" || die "$DEPLOY_DIR does not exist"
[ -f "$COMPOSE_FILE" ] || die "$DEPLOY_DIR/$COMPOSE_FILE not found"
[ -s "$STATE" ] || die "nothing to roll back to: $DEPLOY_DIR/$STATE is missing or empty"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# Only services still defined in the compose file get pinned. A service recorded
# before it was commented out would otherwise land in the override with nothing
# but an image -- no .env, no network, no command -- and compose would start a
# container with exactly that.
defined="$(compose config --services)"

say "Images recorded before the last deploy"
{
  echo "services:"
  while read -r svc ref || [ -n "${svc:-}" ]; do
    [ -n "$svc" ] || continue
    if grep -qx "$svc" <<<"$defined"; then
      printf '  %s:\n    image: %s\n' "$svc" "$ref"
      echo "  $svc -> $ref" >&2
    else
      echo "  $svc: skipped, no longer in $COMPOSE_FILE" >&2
    fi
  done < "$STATE"
} > "$OVERRIDE"

grep -q "image:" "$OVERRIDE" || die "none of the recorded services is still in $COMPOSE_FILE"

cat <<'WARN'

This reverts the IMAGES ONLY. The database is not touched.

If the deploy being undone applied a migration, the older image may not
understand the newer schema. Then the fix is restoring the dump that
deploy-portfolio-ai took before migrating -- see backups/ -- not this.
WARN

if [ "$ASSUME_YES" -ne 1 ]; then
  # No terminal to answer from (a script, cron) reads end-of-input: that is a no.
  read -r -p "Proceed? [y/N] " reply || reply=""
  case "$reply" in
    y|Y) ;;
    *)   die "aborted, nothing changed" ;;
  esac
fi

say "Starting on the recorded images"
compose -f "$OVERRIDE" up -d

# The same checks as the deploy. The worker has to come up and stay up.
sleep 5
worker_cid="$(compose ps -q worker 2>/dev/null || true)"
[ -n "$worker_cid" ] || die "worker container was not created -- see: docker compose logs worker"
worker_state="$(docker inspect --format '{{.State.Status}}' "$worker_cid")"
[ "$worker_state" = "running" ] || die "worker is '$worker_state' -- see: docker compose logs worker"
echo "  worker: running"

# And the API, if this compose file defines one, has to become healthy and then ready.
if grep -qx api <<<"$defined"; then
  api_cid="$(compose ps -q api 2>/dev/null || true)"
  [ -n "$api_cid" ] || die "api container was not created -- see: docker compose logs api"
  api_health="starting"
  for _ in $(seq 1 30); do
    api_health="$(docker inspect --format '{{.State.Health.Status}}' "$api_cid")"
    [ "$api_health" = "starting" ] || break
    sleep 2
  done
  [ "$api_health" = "healthy" ] || die "api is '$api_health' -- see: docker compose logs api"
  compose exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5)" \
    || die "api is up but not ready -- usually the database; see: docker compose logs api"
  echo "  api: healthy and ready"
fi

say "Rolled back"
echo "Pinned until the next deploy-portfolio-ai, which moves forward on :latest again."
echo "Fix the bad build before deploying, or the next deploy brings it straight back."
```

### Install

```bash
sudo nano /usr/local/bin/rollback-portfolio-ai     # paste the script above, save
sudo chmod 0755 /usr/local/bin/rollback-portfolio-ai
```

It pins images through a Compose override rather than editing `docker-compose.yml`, so the override
is not sticky: the next `deploy-portfolio-ai` runs without it and moves forward on `:latest` again.
Rollback is a way to stop the bleeding, not a state to live in — fix the bad build before deploying.

Three things it guards against that the first version did not:

- **A service that has since been commented out is skipped**, not pinned. Otherwise it would land
  in the override with nothing but an image — no `.env`, no network, no command — and Compose would
  start a container with exactly that.
- **No terminal to answer from counts as "no".** Run from a script without `--yes`, it says
  `aborted, nothing changed` rather than exiting silently.
- **The API is checked as well as the worker**, the same way the deploy checks it. A rollback that
  left the API unhealthy would otherwise report success.

The first rollback after the API release is a special case. Before that release the API was not
running, so no image was recorded for it: the rollback pins the worker only, and the API stays on
the new image. If the API is the thing to undo, `docker compose stop api` takes it out of service.
Nothing calls it until the site's own API does, in build step 6.

---

## 11. Operations

### Logs

Everything is JSON, one object per line, on stdout — collected by Docker.

```bash
cd /opt/portfolio-ai
docker compose logs -f worker
docker compose logs --since 24h worker | grep -v '"level":"info"'
docker compose logs -f api
```

The API writes one `request` line per request -- method, route, status, duration -- and never the
body. The route is the one the code declares, such as `/v1/messages/{message_id}/feedback`, never
the path as sent, which could hold anything the caller put in it; `null` means no route matched.
Each carries a `request_id`, the same id the caller receives in the `X-Request-ID` header, and so
does every line logged while serving it, including the
answer's own `llm_call` and `turn_answered` lines, which can arrive after the request line if the
visitor left early. Docker's health probes are logged at debug, so they do not appear at `INFO`.

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
docker compose logs --since 12h worker
```

An ingestion run that found nothing to do is the normal case — `content_hash` means an unchanged
document is not re-embedded, so the daily cost after the first run is close to zero.

**Verify `TZ` actually took effect**, because a wrong timezone is silent and shifts every job by an
hour for half the year:

```bash
docker compose exec worker date
```

Manual runs, when you do not want to wait for the schedule:

```bash
docker compose run --rm worker python -m portfolio_ai.ingestion --dry-run
docker compose run --rm worker python -m portfolio_ai.ingestion --force
```

> **`--dry-run` first, every time, against production.** It plans and prints and writes nothing,
> and it is the only cheap way to find out that a change to chunking is about to re-embed the
> entire corpus.

### The API

**The daily spending limit.** `DAILY_SPEND_CAP_USD` is compared with what answers cost over the
last 24 hours (a rolling window, not a calendar day). At or past it, every message gets the fixed
"back tomorrow" reply instead of an answer, and a `daily_spend_cap_reached` warning is logged.
Setting it to `0` turns the chat off entirely, without a deploy. What was spent:

```bash
docker exec <postgres-container> psql -U portfolio_ai -d portfolio_ai -c "
  select coalesce(sum(cost_usd), 0) as last_24h_usd, count(*) as answers
  from portfolio_rag.chat_messages
  where role = 'assistant' and created_at > now() - interval '24 hours';"
```

**Changing any setting** means editing `.env` and then `docker compose up -d api`, which recreates
the container with the new environment. `docker compose restart api` does not: a restart keeps the
environment the container was created with, and looks as if it worked.

**What the statuses mean**, when the Express API starts reporting them: 409 is a visitor sending a
second message before the first answer finished; 429 is one conversation past 20 messages in 15
minutes; 503 is OpenAI or the database unavailable, or `MAX_CONCURRENT_TURNS` answers already in
flight. None of them needs action unless it is constant.

### The retention sweep

`python -m portfolio_ai.analytics purge` runs at 03:00 and deletes chat older than
`CHAT_RETENTION_DAYS` (90). Its log line is `retention_purged`, with counts. To see what it would
delete without deleting it:

```bash
docker compose run --rm worker python -m portfolio_ai.analytics purge --dry-run
```

### Asking the assistant something, on the server

The quickest way to prove that the production key, the production database and the production
knowledge base all work together — before any of it is reachable from the site:

```bash
docker compose run --rm worker python -m portfolio_ai.assistant --no-save -m "What does Mihail do?"
```

It prints the answer, the route it took, what the best chunk scored and what the answer cost.

**`--no-save` is not optional here, and the command enforces it.** With `ENVIRONMENT=production`
it refuses to run without the flag, because anything it wrote would land in the same tables as
real visitors' conversations and could not be told apart from them afterwards.

### Backups

`deploy-portfolio-ai` dumps `portfolio_rag` before any migration and keeps 30 days of them in
`/opt/portfolio-ai/backups`. That covers the deploy-shaped risk. It does **not** cover the box
dying, so if the Postgres container is not already in a host-level backup, that is a separate
problem and a more important one.

Restore is deliberately manual:

```bash
# Into a scratch schema first, always. Never straight over the live one.
docker exec -i <postgres-container> pg_restore \
    -U postgres -d portfolio_ai --no-owner --no-privileges \
    --schema=portfolio_rag < /opt/portfolio-ai/backups/portfolio_rag-<stamp>.dump
```

> The dump contains columns of type `public.vector`. Restoring into a database without the
> extension fails on the first `CREATE TABLE` with `type "vector" does not exist` — which reads
> like a corrupt dump and is not. §5f again.

### Collation version mismatch

Found while creating the database: the Postgres data directory on this server was initialised
under **glibc 2.41** (Debian 13, "trixie") and the container now runs on **glibc 2.36** (Debian
12, "bookworm"). The image was switched to an older base at some point — a `…-bookworm` tag in
place of a trixie one, or a floating tag that resolved differently.

`portfolio_ai` is unaffected: it was created from `template0` with Postgres's own builtin
locale (§5e), which glibc does not touch. **The other databases on the server are affected**,
including n8n's, and this is worth looking at separately, soon. To see every database's recorded
version against what the OS now provides:

```sql
select datname,
       datcollversion                         as recorded,
       pg_database_collation_actual_version(oid) as actual
from pg_database
where datcollversion is not null;
```

Why it matters: B-tree indexes on text are stored in collation order. Where glibc changed the rules
between versions for characters present in the data, those indexes can disagree with how the server
now compares text — lookups that miss rows that exist, unique constraints that admit duplicates.
For mostly-ASCII data it is often harmless in practice, but nothing guarantees it, and Postgres logs
a warning on every connection to an affected database for that reason.

The fixes, in order of preference:

1. **Run the Postgres container on an image matching the data** — a trixie-based tag of the same
   Postgres and pgvector version. The mismatch disappears for every database at once, with no data
   touched. This is a change to the Postgres container, which n8n depends on, so back up first.
2. **Or stay on bookworm and repair each affected database**: `reindex database <name>;` to rebuild
   text indexes under the current rules, then `alter database <name> refresh collation version;`.
   Refreshing *without* reindexing only silences the warning.

Do not `alter database template1 refresh collation version` as a shortcut — if option 1 is taken
later, it would put `template1` out of step in the other direction.

### Cost

Two things spend money, and neither is bounded by anything in Docker:

- **Ingestion** re-embeds only what changed, so a normal day is effectively free. A `--force` run
  re-embeds all eleven documents, which is fractions of a cent.
- **Chat** is the real number, and it scales with visitors. `DAILY_SPEND_CAP_USD` (default $1.00
  a day, about 250 answers) is the actual protection — on breach the API returns a polite refusal
  rather than an error. Set it before the chat is reachable from the site, not after. Behind it,
  set a monthly budget on the OpenAI project itself: the one limit that holds even if this code
  is wrong.

### Rotating the API key

`PORTFOLIO_AI_API_KEY` appears in two `.env` files on this box: this project's and the Express
API's. They must match, and changing one without the other returns 401 to every visitor.

```bash
openssl rand -hex 32
# edit both .env files, then restart both:
docker compose -f /opt/portfolio-ai/docker-compose.yml up -d api
docker compose -f <portfolio-dir>/docker-compose.yml up -d api
```

---

## 12. Cutover from n8n

Until cutover, **both systems run side by side and neither notices the other.** n8n keeps serving
the live chat from `mihaylov_rag_documents`; this project writes only to `portfolio_rag`. That is
the whole point of the separate schema, and it means the vector store here can be built, ingested
hourly and left to prove itself for weeks before anything is switched.

The switch has three parts, in the portfolio repo (`mmihaylov94/my-portfolio`):

- **the proxy**: `/api/chat` and `/api/chat/feedback` in its Express API, which ship first and
  switched off. The contract they meet is [API.md](API.md#what-the-proxy-has-to-do-build-step-6);
- **the new chat UI**, with `@n8n/chat` removed;
- **the rewritten `knowledgebase/projects/portfolio-ai-assistant.md`**, which ships with the UI.
  It describes the n8n system until then, and must not change earlier: n8n re-indexes `main`
  every week, and the old bot would start describing itself as this one.

In order, on the server unless it says otherwise:

1. **Preconditions.** portfolio-ai is on its latest image; ingestion is running hourly (the
   worker's `ingestion_complete` lines, 11 documents); `DAILY_SPEND_CAP_USD` is set (1.00); the
   OpenAI project has a monthly budget (§11, Cost).
2. **Let Traefik trust Cloudflare.** Cloudflare's proxy is on in front of mihaylov.io, so every
   request reaches Traefik from a Cloudflare address, and by default Traefik replaces
   `X-Forwarded-For` with that address. Nothing behind it can then tell visitors apart. In
   Traefik's static configuration, give the entrypoint the site uses (`websecure`)
   `forwardedHeaders.trustedIPs`, listing Cloudflare's ranges as published at
   <https://www.cloudflare.com/ips/>, then recreate Traefik so it reads its static configuration
   again, whether that lives in its compose `command:` or a mounted file:
   `docker compose up -d --force-recreate` on its service. Worth doing at the same time: allow
   inbound HTTPS to the server only from those ranges, so the origin cannot be reached around
   Cloudflare.
3. **Deploy the Express API, switched off.** In the portfolio's server `.env`, set
   `TRUST_PROXY=2` (Cloudflare, then Traefik) and leave `PORTFOLIO_AI_URL` unset. In the
   portfolio's directory, `docker compose pull api && docker compose up -d api`: `restart` would
   keep the old container, the old image and the old `.env`. Its first log line should read
   `"event":"api_started"`, `"chat":"disabled"`, `"trust_proxy":"2 hops"`, with no
   `trust_proxy_*` or `portfolio_ai_*` problem lines before it. Then prove the visitor address is
   right, from outside, with the rate-limit headers. The chat routes answer 503 while off, but the
   limiter runs first and counts every request:

   ```bash
   curl -si -X POST https://mihaylov.io/api/chat/feedback -H 'Content-Type: application/json' -d '{}' | grep -i '^ratelimit:'
   ```

   Run it three times: `remaining` should fall by one each time. From another network (the same
   laptop on a phone's hotspot) it should start again from the top. Back on the first network,
   add `-H 'X-Forwarded-For: 192.0.2.1'`: `remaining` should keep falling in the same bucket. If
   every network shares one count, Traefik is not trusting Cloudflare yet (step 2).
4. **Switch the proxy on.** Add `PORTFOLIO_AI_URL=http://portfolio-ai:8000` and
   `PORTFOLIO_AI_API_KEY` to the portfolio's server `.env`. The key must be identical to this
   service's own. `docker compose up -d api` there; the start line now says `"chat":"enabled"`.
   Two checks, neither of which stores anything or spends anything:

   - **Feedback on an answer that does not exist is a 404.** That proves Traefik, Express, the key
     and the database in one call:

     ```bash
     curl -s -X POST https://mihaylov.io/api/chat/feedback -H 'Content-Type: application/json' -d '{"session_id":"cutover-check-0001","message_id":1,"rating":1}'
     # {"error":"There is no such answer in this conversation."}
     ```

     A 502 with "unavailable" means the two keys differ.
   - **A refusal streams all the way through Cloudflare.** Set `DAILY_SPEND_CAP_USD=0` in this
     service's `.env` and `docker compose up -d api` here. Before sending anything, check the
     container really reads it: `docker compose exec api printenv DAILY_SPEND_CAP_USD` must print
     `0`, because with any other value the question below is answered, stored and paid for, which
     is a test chat through production. Then:

     ```bash
     curl -sN -X POST https://mihaylov.io/api/chat -H 'Content-Type: application/json' -d '{"session_id":"cutover-check-0001","message":"Hello"}'
     # : connected
     # event: token   (the daily-limit sentence)
     # event: done    (message_id null)
     ```

     A refusal is decided in admission, which only reads (`deps.py`): no session, no message, no
     OpenAI call. Put the cap back and `docker compose up -d api` again. Streaming itself, word
     by word, is proven by the proxy's tests and locally end to end; the first real answer
     confirms it through Cloudflare.
5. **Deploy the new front end.** First record exactly which image the site is running, because
   that is the rollback. `main`'s head is not a safe stand-in: a commit that only touches `api/`
   builds no site image, and the server runs whatever was last pulled. In the portfolio's
   directory:

   ```bash
   docker image inspect "$(docker inspect --format '{{.Image}}' "$(docker compose ps -q site)")" --format '{{index .RepoDigests 0}}'
   # ghcr.io/mmihaylov94/my-portfolio@sha256:...
   ```

   Then merge the chat UI and the rewritten article to `main`, wait for the new image, and
   `docker compose pull site && docker compose up -d site`. The article reaches this service's
   index within the hour.
6. **Watch for a week.** Both systems still work; only the traffic has moved. Look at the
   portfolio API's `chat_stream` lines (outcomes other than `completed` and `client_gone`), this
   service's errors and `daily_spend_cap_reached`, and any `icon_request_reached_api`, which means
   an icon is missing from the site's bundle. **Rollback** is the digest recorded in step 5,
   whose widget still talks to n8n: set the `site` service's `image:` to it and
   `docker compose up -d site`. If the rollback lasts past a Monday, revert the article on `main`
   too, or n8n's weekly re-index teaches the old bot to describe itself as this one.
7. **Then, and only then**, disable the n8n workflows and drop `mihaylov_chat_histories`, in n8n's
   database. `mihaylov_rag_documents` can go once the new ingestion has a month of clean runs
   behind it. Delete the portfolio's `NUXT_PUBLIC_N8N_CHAT_WEBHOOK_PATH` secret at the same time.
8. **golden_v2.** golden_v1's `assistant-how` and `n8n-work` cases describe the n8n system, and
   its Python cases say no portfolio project uses Python. They change in a new dataset version,
   written against the rewritten articles once they are indexed, with its own baseline run.

The knowledge base article changes **in the same change as the front end**: otherwise the
assistant describes itself inaccurately to the people asking about it. (Its claims of thumbs
up/down feedback, reCAPTCHA and an Express proxy, none of which the n8n bot has, were removed in
the fact-check of 2026-09-23.)

---

## 13. Gotcha index

- **`vector` is an untrusted extension** — a non-superuser cannot create it, only skip it when it
  already exists. Check with `select extname from pg_extension where extname='vector'` before the
  first deploy. §5f.
- **Never `drop extension vector`** to reinstall it — it cascades to every vector column in the
  database. In `portfolio_ai` that is only our embeddings, which is one reason it is a
  separate database from n8n's.
- **`ENVIRONMENT=production`, not `local`.** Two guards key off it; `local` would reject a
  port-5432 URL and would let the test fixture believe it may drop schemas here.
- **Port 5432 in production, 5433 locally.** The 5433 rule is a local-only guard for a local-only
  hazard — two databases on one host, only one with pgvector.
- **`DATABASE_URL` uses the Postgres service name**, not `localhost` and not an IP. The container
  is not the host.
- **A misspelt `.env` key is ignored on this box, not rejected.** Locally `extra="forbid"`
  refuses it; in the containers the file arrives as environment variables and an unknown one is
  never looked at, so the setting keeps its default. §8 step 2 prints what the containers read.
- **`docker compose restart` does not re-read `.env`.** `docker compose up -d` does.
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
- **`TRUST_PROXY=2` on the portfolio's API only once Traefik trusts Cloudflare.** Before that,
  every visitor arrives from a Cloudflare address and the per-visitor limits are per edge. §12
  step 3 checks it with the rate-limit headers.
- **`--dry-run` before any production ingestion.**
- **`DAILY_SPEND_CAP_USD` before the chat is reachable**, not after.
- **No routing labels on `portfolio-ai`, and `traefik.enable=false`** — it is internal, and
  exposing it would make the bearer token the only thing between the internet and an OpenAI bill.
- **`stop_grace_period: 45s` on the api.** Docker's default of 10 s would cut answers off
  mid-sentence on every deploy; the API needs up to 40 s to finish them.
- **No test chats through the production API.** Every answer it gives is stored with the
  visitors' conversations. Check it with the non-writing calls in §8, and check OpenAI with the
  terminal chat's `--no-save` (§11).
- **`DAILY_SPEND_CAP_USD=0` turns the chat off**, then `docker compose up -d api`.
- **Docker log rotation applies at container creation.** Changing `daemon.json` and restarting the
  daemon leaves existing containers uncapped while looking done.
- **A private GHCR package needs a PAT that expires.** A deploy failing with `denied` long after
  everything worked is usually that.
