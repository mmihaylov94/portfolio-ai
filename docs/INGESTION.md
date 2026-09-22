# Ingestion, end to end

How the knowledge base gets from Markdown files in a GitHub repository into the tables the
assistant searches: what happens during a run, which module does which part, and what each
library is used for. The reasoning behind the design is in [ARCHITECTURE.md](../ARCHITECTURE.md)
§7. This document is about the code.

## The idea it rests on

**Ingestion is a diff.** Each run works out what has changed in the repository since the last run,
and does only that.

It decides cheaply first and exactly second. One call to GitHub's tree API lists every file along
with its *git blob SHA*, a hash git computes from the file's contents, so an unchanged file is
recognised without being downloaded. A file that does get downloaded is hashed again locally, and
that `content_hash` has the final say on whether its text really changed.

Most of the design follows from taking that seriously.

The run splits in two at the moment the decision is complete. `plan()` talks to GitHub and reads
the database, and writes nothing. `execute()` does everything that costs money or changes state:
embedding, writing, deleting. `--dry-run` runs the first half and stops, which makes a dry run the
same code a real run executes rather than a separate description of it that could drift.

It is also why the schedule can be hourly. When nothing has changed, a run makes one request to
GitHub and a few small queries, embeds nothing and spends nothing. Only the first run on an empty
database does real work. After that, what a run costs is proportional to what was edited.

And it is why one step needs a guard. A diff can conclude that a document was deleted, and the
only evidence is the document's absence — which is also exactly what an incomplete answer from
GitHub would look like. So the step that deletes vanished documents is limited in how much it may
delete at once. See [What protects the knowledge base](#what-protects-the-knowledge-base).

## One run, start to finish

```
plan()                                                    reads GitHub + Postgres, writes nothing
   1  discover  github.list_markdown_files()             one tree call: every path + blob SHA
   2  load      documents.load_index()                   what is already stored, one query
   3  guard     documents.purge_limit()                  stop now if too much looks deleted
   4  diff      pipeline._needs_fetch()                  new path or new blob SHA -> download it
   5  fetch     github.fetch_all()                       only those files, five at a time
   6  parse     frontmatter.parse_document()             DocumentMeta + body, or a rejection
   7  confirm   pipeline.content_hash()                  did the text really change?
   8  chunk     chunking.chunk_document()                one chunk per H2 section
------------------------------------------------------------ --dry-run stops here
execute()                                                 everything that costs money or writes
   9  embed     embeddings.embed_texts()                 per document, up to 64 chunks a request
  10  persist   upsert_document() + replace_chunks()     one transaction per document
  11  touch     documents.touch_indexed_at()             mark unchanged documents as seen
  12  purge     documents.purge_stale()                  delete what vanished, guarded again
  13  report    IngestionReport                          one JSON log line + the CLI summary
```

Follow a run in which one article has been edited since the last one.

It starts with `list_markdown_files()` asking GitHub for the branch's whole tree in a single
request. It keeps only files (entries of type `blob`) under `knowledgebase/` ending in `.md`, and
sorts them by path so logs and plans come out in the same order every time. The tree API has a
size limit, and when a repository exceeds it the response sets `truncated: true` and quietly leaves
files out. The run refuses to continue if it sees that flag, because a truncated tree is
indistinguishable from a repository in which files were deleted.

Next, `load_index()` reads what is already stored — every document's `doc_id`, path, blob SHA and
content hash — in one query, and returns it twice over in a `DocumentIndex`: once keyed by
`doc_id`, once keyed by path. The path view exists because "do I need to download this file?" has
to be answered before the file is read, when its path is all that is known. The first purge check
follows: if even the best case, in which every discovered file turns out fine, would delete more
documents than the limit allows, the run stops before it has downloaded or spent anything.

The diff itself is one line. A file is downloaded if its path has never been stored, if its blob
SHA differs from the stored one, or if `--force` was given. In this run that is one file, and the
other ten are never touched. `fetch_all()` downloads the chosen files from
`raw.githubusercontent.com`, at most `INGESTION_CONCURRENCY` (default 5) at a time.

The downloaded file goes to `parse_document()`, which splits the YAML block from the body and
validates the YAML into a `DocumentMeta`. A file that fails does not end the run. It becomes a
`Rejection` in the report and the run carries on with the rest; what happens to its previously
indexed version is covered under the guards below. This is also where a second file claiming a
`doc_id` that is already taken gets rejected.

Then the text is hashed. If both the hash and the path match what is stored, the blob SHA changed
but the content did not, and nothing is re-embedded. The path is compared too because a renamed
file has identical content at a new location, and its stored path must be updated or the next
run's download decision would be wrong. The hash covers the whole file, frontmatter included, so
editing only `last_verified` re-embeds the article — a fraction of a cent, in exchange for never
having to decide which fields count.

Finally `chunk_document()` splits the body on `## ` headings, one chunk per section, and puts each
heading at the top of its chunk's text as `"{title}\n\n{body}"`. The heading is included because
the knowledge base is written as questions (`## What is Mihail's job title?`), and a visitor's
question looks far more like the heading than like the prose answering it. Two edge cases: text
before the first heading becomes an `intro` chunk, where the n8n version silently dropped it, and an
article with no headings becomes a single `main` chunk. A section estimated at more than 6,000
tokens would be split at paragraph breaks. The largest in the corpus is under 600.

That is the end of `plan()`. It hands back an `IngestionPlan`: the documents to write, with their
chunks; the ones found unchanged; every `doc_id` seen; what was rejected; and what would be purged.

`execute()` works through the pending documents one at a time. For each, `embed_texts()` turns its
chunks into vectors, and then a single database transaction upserts the document's row and
replaces all its chunks. One document at a time, so that a failure part way through leaves every
earlier document complete. One transaction per document, so that a failure between the two
statements can never leave a document without chunks: a state that looks healthy in the
`documents` table while the article is invisible to search.

Unchanged documents then get their `indexed_at` set to this run's time, marking them as still
present. `purge_stale()` deletes every stored document that was not seen, which in this run is
none. The run ends with an `IngestionReport`, logged as one JSON line and printed as a summary.

## What ends up in the database

Two tables in the `portfolio_rag` schema, from migration 0001.

`documents` has one row per article. `doc_id` is its identity, the key every upsert and purge
works against, and it comes from the frontmatter so it survives renames. Beside it are the
frontmatter fields (`title`, `url`, `tags`, `last_verified` and so on), and the three columns the
diff needs: `source_path`, `git_blob_sha` and `content_hash`. `indexed_at` records the last run
that saw the article.

`chunks` has one row per section: `chunk_index`, `section` (a slug of the heading),
`section_title`, the `content` that was embedded, and the `embedding` itself, a `vector(1536)`
column with an HNSW index for cosine search. `document_id` references its document with
`on delete cascade`, so deleting a document deletes its chunks and no code has to remember to.
`token_count` is left empty on purpose: the embeddings API reports tokens per request, not per
input, and a column holding an estimate under an exact-sounding name would be worse than a null.

## Which piece does what

| Module | What's in it | Its job |
|---|---|---|
| `ingestion/github.py` | `RemoteFile`, `build_client()`, `list_markdown_files()`, `fetch_all()` | Talk to GitHub |
| `ingestion/frontmatter.py` | `DocumentMeta`, `split_frontmatter()`, `parse_document()` | Turn a file into metadata and body, or reject it |
| `ingestion/chunking.py` | `Chunk`, `chunk_document()`, `slugify()` | Split a body into sections worth embedding |
| `ingestion/pipeline.py` | `IngestionPlan`, `PendingDocument`, `Rejection`, `IngestionReport`, `plan()`, `execute()`, `run()` | The run itself |
| `ingestion/cli.py`, `__main__.py` | `main()` | `python -m portfolio_ai.ingestion`: flags, summary, exit code |
| `ingestion/validate.py` | `Finding`, `main()` | `portfolio-ai-validate`, run by the portfolio repo's CI |
| `db/documents.py` | `IndexedDocument`, `DocumentIndex`, `DocumentWrite`, `load_index()`, `upsert_document()`, `replace_chunks()`, `touch_indexed_at()`, `purge_limit()`, `purge_stale()` | Every SQL statement for `documents` and `chunks` |
| `llm/embeddings.py` | `EmbeddingResult`, `embed_texts()` | Text in, vectors and their cost out |
| `llm/client.py` | `get_client()`, `close_client()` | The one shared OpenAI client |
| `llm/pricing.py` | `PRICES`, `cost_usd()`, `estimate_tokens()` | What a call cost, and rough token estimates |
| `concurrency.py` | `gather_limited()` | Many downloads at once, with a ceiling |
| `cli.py` | `run_async()` | Logging, the event loop and exit codes, for every command |

`config.py`, `logging.py` and `db/pool.py` sit underneath all of it and are covered by the lessons
in [docs/lessons/](lessons/).

The classes come in three kinds, and the kind tells you something. Values that never change once
made are **frozen dataclasses**: `RemoteFile`, `Chunk`, `IndexedDocument`, `DocumentWrite`,
`PendingDocument`, `Rejection`, `EmbeddingResult`. The `@dataclass` decorator writes `__init__`,
`__eq__` and `__repr__` from the annotated fields, and `frozen=True` makes assigning to a field an
error. The two things filled in as the run goes, `IngestionPlan` and `IngestionReport`, are
ordinary mutable dataclasses. And the one thing that arrives from outside and must be checked,
each article's frontmatter, is a **Pydantic model**, `DocumentMeta`. A dataclass does no checking
at all: pass it the wrong type and it stores it. Pydantic validates and converts. In PHP you
would probably write all three as plain classes; here the choice follows a project rule, Pydantic
where data crosses a boundary and dataclasses everywhere else.

## The libraries, and what each is for

**httpx** is the HTTP client, in `github.py`. `build_client()` creates one `httpx.AsyncClient`
with the headers GitHub asks for, an optional token, timeouts, and `follow_redirects=True`, because
raw file URLs can redirect and without it the body would be an empty redirect response. `plan()`
opens it with `async with github.build_client() as client:`, and the client's connections are
closed when that block ends, however it ends. `async with` is a context manager, Python's general
way of saying "set this up, and guarantee the tear-down", and the same construct opens and commits
database transactions later in the run. Because the client is created in one place and passed in,
the tests can swap it for one whose transport is an `httpx.MockTransport`, a function that answers
requests instead of the network.

**asyncio**, from the standard library, runs the downloads concurrently through
`gather_limited()` in `concurrency.py`, which is `asyncio.gather` with a `Semaphore` as a ceiling.
It is worth being precise about what concurrency means here, because it is not threads. asyncio
runs everything on one thread and switches between tasks only where the code says `await`, so
"five downloads at once" means five requests in flight while one thread waits on all of them. That
is the same model as `Promise.all` in Node. PHP-FPM has nothing like it: one request there is one
straight line of execution. Results come back in the order the downloads were started, not the
order they finished, so they can be matched back to their paths by position.

**PyYAML** reads the frontmatter with `yaml.safe_load`, never `yaml.load`. The unsafe loader can
build arbitrary Python objects from tags written in the document, and this text arrives over the
network. `safe_load` still returns real types, so `tags` arrives as a list and `last_verified` as
a `datetime.date`.

**Pydantic** validates that result into `DocumentMeta` with `DocumentMeta.model_validate()`.
`doc_id` and `title` are required and cannot be empty. `tags` accepts a YAML list or a
comma-separated string, normalised by a `field_validator`. Unknown keys are ignored
(`extra="ignore"`), so an article can carry metadata for other purposes without being rejected.
A `ValidationError` is caught and becomes a `DocumentRejectedError` naming the fields at fault.

**The OpenAI SDK** does the embedding. `llm/client.py` builds one `AsyncOpenAI` client from the
settings: the key, a 30-second timeout, and up to three automatic retries for connection errors,
timeouts, rate limits and server errors. It is cached with `functools.lru_cache`, so every caller
shares one pooled HTTPS connection. `embed_texts()` calls
`client.embeddings.create(input=batch, model="text-embedding-3-small", dimensions=1536)` with up
to 64 texts per request, then sorts the results by their `index` field, because the API does not
promise to return them in the order they were sent. It checks that the number and width of the
vectors match what was asked for, adds up the token usage the API reports, and prices it in
`llm/pricing.py` as a `Decimal`, never a float. A rejected API key becomes a one-line
`ConfigError` rather than a forty-line traceback.

**psycopg 3 and psycopg_pool** are the Postgres driver and its connection pool. The pool, from
step 1, hands out connections whose `search_path` already points at `portfolio_rag` and whose rows
come back as dictionaries. Ingestion leans on four of the driver's features:

- `async with pool.connection() as conn, conn.transaction():` commits when the block finishes and
  rolls back if anything inside it raises. It is Laravel's `DB::transaction(function () { ... })`,
  written as a block.
- `insert ... on conflict (doc_id) do update ... returning id` is the upsert. It keeps a document's
  primary key stable across runs, where n8n's delete-then-insert gave it a new one every time.
- `executemany` sends all of a document's chunks in one pipelined exchange rather than one round
  trip per row, which is noticeable with the database on another machine.
- A Python list passed as a parameter becomes a Postgres array, so `where doc_id = any(%s)` is one
  placeholder however many ids it holds.

Every value goes in through a `%s` placeholder. Nothing is ever formatted into the SQL text.

**pgvector** supplies the `vector` column type and the index. `register_vector_async()`, run once
per connection in `db/pool.py`, is what lets a Python list of floats be written into a vector
column. Ingestion only writes vectors; searching them is the assistant's job, described in
[ASSISTANT.md](ASSISTANT.md).

**typer** builds both command lines from the function signatures. A parameter declared as
`dry_run: Annotated[bool, typer.Option("--dry-run", help="...")] = False` becomes a `--dry-run`
flag with help text, and the type hint is all it needs to know it is a flag.

**structlog** writes each step as an event name with fields, such as
`log.info("document_indexed", doc_id=..., chunks=..., tokens=...)`, rendered as one JSON object
per line so a week of runs can be searched and counted.

The rest is standard library: `hashlib.sha256` for the content hash, `re` to find the headings,
`decimal` for money, and `datetime` with an explicit timezone throughout (`dt.datetime.now(dt.UTC)`),
because a naive timestamp compared with an aware one is a bug waiting for a clock change.

## What protects the knowledge base

**The purge is limited to a share of what is stored.** A run may delete at most
`INGESTION_MAX_PURGE_FRACTION` (0.3) of the stored documents, or `INGESTION_PURGE_GRACE` (2),
whichever is larger; `purge_limit()` computes it. With 11 articles the limit is 4, and with 50 it
would be 15. A fixed number was tried first and rejected, because it stops protecting as the corpus
grows. The check runs twice: optimistically in `plan()`, before anything is downloaded, and
exactly in `purge_stale()`, which counts and deletes inside one transaction so the count checked
is the count deleted. Past the limit the run raises `PurgeSafetyError` and deletes nothing.

**A file that fails to parse keeps its last good version.** The file still exists upstream, so its
previously indexed version counts as seen and is not purged. The old version keeps answering, its
`indexed_at` is deliberately not advanced so it shows up as stale, and the run exits non-zero.
The alternative would let a typo in one article's frontmatter remove that article from the
assistant's knowledge.

**Two files cannot share a `doc_id`.** Without a check, both would be embedded and the second would
silently overwrite the first's row, reporting two documents indexed and storing one. The later
file, by path order, is rejected instead. The check also covers files that were not downloaded
because they were unchanged, so a new file cannot quietly take over an untouched article's
identity.

**A truncated tree stops the run**, as described above, because it would look like deletions.

**`--only` skips the purge.** It restricts a run to named `doc_id`s for debugging, so the set of
documents seen is deliberately not the set that exists.

## Checking articles before they land

`portfolio-ai-validate` lets the portfolio repository catch a broken article in review rather than
in an ingestion log. It runs the same `parse_document()` and `chunk_document()` a real run uses, so
"passes the checker" and "will be indexed" cannot drift apart, and it needs no database, network
or configuration. That is what lets the portfolio repo's `.github/workflows/knowledgebase.yml` run
it from the published image on every commit that touches `knowledgebase/`.

It treats as errors anything that would be rejected, a duplicate `doc_id`, a `url` containing
`/knowledgebase/` or `/projects/`, a `last_verified` date in the future, and an article with no
content. It warns, without failing, about a missing `url` or `last_verified`, an article with no
`##` headings, and a section big enough to be split. `--strict` makes the warnings fail too.

```bash
uv run portfolio-ai-validate ../my-portfolio/knowledgebase
```

```
11 article(s) checked, 11 distinct doc_id(s), 0 error(s), 0 warning(s).
All good.
```

## How it is tested

The rule the tests follow is to fake what leaves the machine and keep the database real.

The unit tests cover the pure functions — frontmatter parsing, chunking, pricing, the purge limit
and the validator — with no database and no network. The integration tests run against a real
Postgres in a throwaway `pytest_*` schema created from the real migrations. `test_documents.py`
runs the SQL: the upsert keeping its primary key, the cascade, the purge refusing. `test_pipeline.py`
runs whole ingestion passes with GitHub replaced by `FakeRepo`, which serves a dictionary of paths
and contents through an `httpx.MockTransport` and derives blob SHAs from the contents so an edit
changes the SHA as it would in git. `embed_texts` is replaced by a function that returns vectors of
the right shape and counts what it was asked to embed, which is how "a second run embeds nothing"
gets tested.

## Running it

```bash
uv run python -m portfolio_ai.ingestion --dry-run            # plan only: no writes, no spend
uv run python -m portfolio_ai.ingestion                      # incremental run
uv run python -m portfolio_ai.ingestion --force              # re-embed everything
uv run python -m portfolio_ai.ingestion --only about-mihail  # named doc_ids only, no purge
```

`--force` is for after a change to chunking. The content hash describes the source file, not how
it was split, so without it a new chunking rule would never reach articles nobody has edited.

A dry run against the development database on 2026-09-22, with nothing changed upstream:

```
PLAN (nothing was written)
  discovered     11
  indexed        0
  unchanged      11
  chunks         0
  tokens         0 (estimated)
  cost           $0.000000 (estimated)
  took           1.4s
```

In a dry run, "indexed" means "would index" and the tokens and cost are estimates. After a real
run they are the API's own figures. Lines for rejected and purged documents appear when there are
any, each rejection saying whether a previous version was kept.

The exit code is 0 for a clean run, 1 if any document was rejected or the run was refused (the
purge guard, bad configuration), and 130 for Ctrl-C. In production, `docker/crontab` runs it
hourly in the worker container, and [DEPLOYMENT.md](DEPLOYMENT.md) covers running it there by hand.
