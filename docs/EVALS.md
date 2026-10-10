# The evals, end to end

How the eval harness scores Rachel against a golden dataset, module by module:
- what it measures, and how
- how a run is stored so that it can be compared months later
- how to read a comparison without being fooled by noise

The code is in `src/portfolio_ai/evals/`, the SQL in `db/evals.py`, and the cases in
`datasets/`, where `golden_v2.yaml` is the current dataset. ARCHITECTURE.md §9 has the design
this implements.

## The idea it rests on

**An eval run is an experiment: change one thing, hold everything else still, and look at every
case that moved.** Every design decision below follows from that one sentence.

- **The cases are held still.** A dataset freezes once a run of it completes: the file is
  hashed, and a changed file under the same name is refused. Scores on different cases are not
  comparable, however alike the numbers look.
- **Everything else is written down.** Each run records its full configuration:
  - the models and reasoning efforts
  - `top_k` and the number of search rounds
  - every prompt's version, the judge's included
  - a fingerprint of the knowledge base
  - the git commit it ran

  A score that moved can then be traced to whatever differed.
- **The ruler is held still too.** The judge's model is pinned separately from the model under
  test (`JUDGE_MODEL`), and its rubric is versioned like any prompt. Trying another chat model
  changes what is measured, never how. When the ruler itself has to change, a new judge model
  or a new rubric, the stored baseline is graded again by the new one first (`rejudge`): scores
  from two judges are not comparable.
- **Look at cases, not only averages.** Models do not answer the same question the same way
  twice. On fifty cases, one answer changing its mind moves an average by two points. `compare`
  lists the cases that changed. When an average moves and no case changed much, that is noise.
  Three cases whose faithfulness fell from 5 to 2 is a finding.

## One run, start to finish

The smoke run below proved the pipeline live before any of this was handed over. It used a
separate, throwaway dataset of three cases, one per route, not golden_v1, and ran against the
development database. Its rows were deleted afterwards.

```
[  1/3] weather                         out_of_scope    F- C- S-         1.9s
[  2/3] who-are-you                     small_talk      F- C5 S5         4.4s
[  3/3] laravel                         mihail_related  F5 C5 S5        21.6s
```

This command ran the first baseline, golden_v1's, in two phases:

```bash
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label baseline \
    --chat-effort default --classifier-effort default
```

**Prepare** (`runner.prepare`) is every check that can refuse a run, and it spends nothing:
- every model has a price in `llm/pricing.py`;
- the chat model and the classifier each take the effort they are paired with;
- the label is free;
- the dataset is not a changed copy of one that is frozen;
- any `--only` cases exist;
- every document a case expects is actually indexed.

The price check catches a misspelt model. OpenAI's "no such model" is an ordinary per-case
failure, so without the check the run would carry on: every answer paid for, none graded, the
label used up. The indexed-documents check exists because a stale local knowledge base would
otherwise show up as retrieval misses that have nothing to do with retrieval. It proves a
document is there, not that it is current: when a dataset was written against edited articles,
push them and run ingestion before the run that freezes it, or that run grades answers against
the old text.

The plan is printed: the effective configuration, the corpus, and where the dataset stands. That
is new, changed (its stored cases will be replaced), or unchanged, with how many complete runs
freeze it. `--dry-run` stops here.

**Execute** (`runner.execute`) runs everything else:
1. It stores the dataset (`db/evals.sync_dataset`). It refuses if the dataset is frozen and has
   changed, the same check prepare made, made again inside the transaction that writes it.
2. It opens the run with its configuration.
3. It answers every case, a few at a time.
4. For each case, it calls `agent.respond()`, the assistant minus memory and storage. It never
   touches the chat tables, which is why evals can ask thousands of questions and leave no
   trace where visitors' conversations live.
5. It keeps the chunks from each `Searched` event and the `TurnResult` from `Done`, and scores
   the answer:
   - retrieval
   - the rules
   - the judge, in a separate call
6. It writes each result as it lands.
7. When every case has finished, it reads the results back, totals them, and marks the run
   `complete`.

A case that fails in the expected way (OpenAI unavailable for a moment) is recorded with its
error, and the run carries on. A failure that would hit every case the same way stops the run,
because the other forty-nine answers would not be worth paying for. Examples are a rejected API
key or a bug. Ctrl-C stops it too. Either way the run is marked `failed`, never left `running`.

## The dataset

`datasets/golden_v2.yaml` is the current dataset: 62 cases, written from the eleven
knowledge-base articles as they stood on 2026-10-09.

| Kind | Cases | What it checks |
|---|---|---|
| One-document questions, and three false premises ("Where did Mihail do his PhD?", "Is Glotsmith an AI chatbot?", "Is this chat built with n8n?") | 39 in all | retrieval, completeness, facts |
| Questions spanning documents ("Which projects use PostgreSQL?") | 3 of those 39 | recall across several documents |
| Not in the knowledge base (salary, rates, age) | 5 | the fallback, no guessing |
| Follow-ups with the previous exchange ("yes please", "And Python?") | 4 | the classifier's context |
| Small talk, including "Who are you?" and "Are you Mihail?" | 5 | route, persona |
| Out of scope | 5 | route, the fixed reply |
| Adversarial: prompt injection, "pretend to be Mihail", a `/projects/` link, "tell me everything" | 4 | rules, persona, length |

**golden_v1 is the first dataset**: 54 cases written from the articles as they stood on
2026-09-25, and the one every run in Results before 2026-10-09 used. It is frozen, and stays in
the repository as the record of what those runs were asked. The cutover from n8n rewrote six
articles, and golden_v2 is golden_v1 with what that changed:

- **Six cases rewritten**, because the right answer changed:
  - `python` and `followup-and-python`: Python is in his stack now, through the assistant;
  - `ai-experience`: the evaluation harness joins what he has built;
  - `open-source`: the assistant's repository joins the public ones;
  - `assistant-how` and `n8n-work`: the assistant is a Python service, and n8n ran its first
    version.
- **Eight cases added**, on what the rewritten assistant article says and nothing tested: how it
  was built, whether it is built with n8n, which projects are in Python, what happens to a
  visitor's messages, which models it uses, whether its source is public, how its quality is
  measured, and whether AI was used to write it.
- **The other 48 are unchanged**, word for word.

`compare` refuses a run of one against a run of the other, because 14 of their cases differ. So
golden_v2 has a baseline of its own, and nothing measured on golden_v1 carries over as a number.

A case can carry these fields:
- **`expected_doc_ids`** lists every article with a section that answers the question on its
  own, even briefly. For a question whose answer is a list, an article giving one item counts
  too. A passing mention in a section about something else doesn't count. Topics repeat
  across the knowledge base (contact details alone are in five articles), so any one found
  counts as a hit, and recall shows how many were found. Applied unevenly, this skews the
  retrieval scores: an article that answers but isn't listed counts against precision and
  MRR whenever it is retrieved.
- **`also_accept`** names another route that is also right. "Are you Mihail?" is answered
  properly by small talk and by the knowledge-base route alike, so neither counts as a misroute.
- **`must_include` and `must_not_include`** are case-insensitive substrings, and hard rules: a
  phrase a correct paraphrase can miss ("solution architect" for "Solutions Architect", a
  retired job title quoted as retired) fails a good answer, and the comparison then lists it
  as a regression of whatever setting changed. Anything a paraphrase can reword is left to
  the judge.
- **`notes`** say why a case exists when that isn't obvious.

**The contact cases check the address itself.** Mihail has two public addresses: the hiring one
printed on his CV, and one for projects and general inquiries. Both are public by his choice, so
`recruiter-contact` must include the first and `project-inquiry` the second. Neither case forbids
the other address, because a good answer to a recruiter can mention it to say it isn't the one
for hiring; whether it does that properly is the judge's to read. No other email address belongs
in a dataset, and a unit test over every YAML file in `datasets/` fails if one appears. That matters
most for questions promoted from real traffic, which can carry a visitor's address.

`python -m portfolio_ai.evals check datasets/golden_v2.yaml` validates the file without a
database or network, and a unit test runs the same check in CI on every golden file. Validation
is pydantic, with `extra="forbid"`: a misspelt field such as `expected_docs` is an error rather
than a case that tests nothing and passes. Rules that span fields are checked in a
`model_validator`, for example:
- only `mihail_related` questions can expect documents;
- a question the knowledge base cannot answer expects no documents;
- history alternates visitor and Rachel, and ends with her answer.

A key written twice in the same mapping is refused too, before pydantic sees the file. Plain
YAML keeps the second of two equal keys without a word, so a case with `expected_doc_ids`
written twice would be scored on the second, and `extra="forbid"` would never see the first.

**To change a dataset after its first complete run**, copy it to the next version
(`golden_v3.yaml` after golden_v2), rename it inside, and edit the copy. Then make it the default
of `run --dataset` in `evals/cli.py`: a unit test fails while the default is not the newest file,
because a run that forgets the flag would grade today's answers against an older knowledge base.
Write the copy under another name, `draft_v3.yaml` say, and rename it once it has been reviewed:
from the moment a file called `golden_v3.yaml` exists, that test wants it as the default, and a
default is what a run without `--dataset` uses, so one forgotten flag would run an unreviewed
file and freeze it. Until a run completes, edits replace the stored cases freely. That includes
after a run that failed part-way: it has no scores worth protecting, and its partial results
are deleted with the old cases.

## What is measured

### Retrieval, on documents

The chunks the model was shown are reduced to their documents, in the order the model first met
each one (`metrics.ranked_documents`), and the expected documents are looked for in that list:

- **recall** is the share of expected documents found;
- **precision** is the share of documents shown that were expected, which a smaller `top_k`
  should raise;
- **MRR** is one over the position of the first expected document;
- **hit rate** is the share of cases where any expected document was found at all.

It is scored on documents rather than chunks because a question is answered by a document.
Which of its sections came first is a detail.

The first smoke run's Laravel question shows why precision and MRR matter. It found both
expected documents, `about-mihail` third and `tech-stack` fifth, among eight documents shown.
The search ranked Threadline, a PHP project, first. The recall was perfect and the ranking was
not: MRR 0.33, precision 0.25.

### Classification

Accuracy against the expected route (or an `also_accept` one), overall and per category.

### The rules

Deterministic checks of what `rag_agent.md` asks for (`metrics.rule_violations`):

| Rule | Fires when |
|---|---|
| `misrouted` | the route is neither the category nor an accepted alternative |
| `forbidden_link_attempted` | the link filter caught a `/knowledgebase/` or `/projects/` URL: a bare one became the nearest real page, a Markdown link kept only its label. The visitor never saw it, but the prompt did not stop the model |
| `forbidden_link_in_answer` | one got through, which the filter should make impossible |
| `invented_link` | a URL that appears neither in the prompts' allowed list nor in the passages shown. Links are compared by where they go: bold or angle brackets, trailing punctuation, the scheme, `www.` and a final `/` are ignored, and a relative link is resolved against `https://mihaylov.io/`. `mailto:` and `tel:` links are contact details, which the judge checks |
| `missing`, `forbidden_phrase` | `must_include` / `must_not_include` |
| `section_header`, `bullet_list`, `too_long` | the answer style: no headers, prose rather than lists, at most six sentences |
| `unprompted_greeting`, `introduced_self` | "Hi" or "I'm Rachel" when nobody greeted her or asked who she is |
| `spoke_as_mihail` | first-person career phrasing ("I built", "my projects", "I'm Mihail"). **A heuristic**, labelled so. "I'm Mihail's assistant" does not fire it: it is the right answer to "Are you Mihail?" |
| `did_not_decline`, `declined_answerable` | the fallback, below |

### The fallback, and the phrase match behind it

For a question the knowledge base cannot answer, the right answer says so. Two things read an
answer for that:
- `fallback_used`, the phrase match in `postprocess.is_fallback`, which the API stores on every
  answer;
- the judge's `declined`, which recognises any wording.

The rules use the judge's reading where there is one. Across a run, **`phrase_match_agrees`** is
the share of knowledge-base answers where the two agree. That is the measurement
`postprocess.py` says the evals exist to make: every disagreement is an answer the analytics
would misfile.

### The judge

`evals/judge.py` asks `JUDGE_MODEL` to grade each answer against the rubric in
`prompts/judge.md`. Since 2026-10-10 that is `gpt-6.1-sol` with rubric version 2; before, it was
`gpt-5` with version 1, and Results below says what the change did to the scores. It uses the
same `responses.parse` structured call the classifier uses, so the reply is held to a schema:

| Field | Scale | Against |
|---|---|---|
| `faithfulness` | 1-5, or null | the passages Rachel was shown: nothing claimed about Mihail, his work or his site that they don't support. Null only when the answer claims nothing about Mihail |
| `completeness` | 1-5, or null | the reference answer's key facts |
| `style` | 1-5 | the persona and answer style: conversational, 2-4 sentences, third person |
| `declined` | yes / no | whether the answer said it did not know |
| `rationale` | text | the specific problems |

Three details matter.
- **The judge sees the passages, not the knowledge base.** It grades what the answer did with
  what retrieval gave it; retrieval is scored separately.
- **A reply that does not fit is recorded, not raised.** If a score is outside 1-5, or the
  reply is unreadable, the case keeps its answer and its other measurements, and says why it
  has no grade.
- **The fixed out-of-scope reply is not graded.** There is nothing in it to judge.
- **The judge has its own timeout**, `JUDGE_TIMEOUT_SECONDS` (180), passed to
  `responses.parse` for its call alone. `OPENAI_TIMEOUT_SECONDS` (30) is sized for a visitor
  waiting on an answer. `gpt-5` reading twenty passages ran past it on every retry, and the
  first baseline lost three verdicts that way. The limit is per attempt, as the SDK's is, so
  with `OPENAI_MAX_RETRIES=3` one judge call can wait about twelve minutes before it gives up.
  Nobody is waiting on it.

**Null means no claims, not no passages.** A greeting, a refusal or "I don't have that
information" has no faithfulness score, because it says nothing about Mihail. An answer that
makes claims with no passages behind it is the opposite case: nothing supports those claims, so
they are scored as unsupported. Small talk that volunteers facts from nowhere is exactly what
this should catch, and an earlier draft of the rubric let it through as null.

**Links are left out of faithfulness, and the smoke run is why.** In the first run the judge
gave a correct answer faithfulness 1 for linking to `https://mihaylov.io/#about`, a page the
passages never mention. Rachel's prompt lists that link as allowed, and the judge never sees the
prompt. The deterministic `invented_link` rule, which knows the allowed list, had rightly passed
it. The rubric now says every link is checked separately, and the same answer scores 5. That
kind of disagreement between the judge and a rule is worth reading every time: one of them is
wrong, and which one says what to fix.

**What an answer says of a link is not left out.** That exemption turned out too wide. In
golden_v2's baseline two answers pointed visitors to a privacy policy the site does not have,
beside a link that is real, and scored 5: the link was exempt, and what the answer claimed of it
went with it. Rubric version 2 leaves the link itself to the rule and scores the claim: saying
the site has a policy, a page, a section or a note that no passage mentions is unsupported.

**The one-sentence fallback is not marked down.** The prompt prescribes the "I don't have that
information in my knowledge base" reply word for word, and rubric version 1 scored it 3 for
style three times in one run, for being a single sentence with no offer of more. Version 2 says
not to.

**Changing the judge.** A judge is its model and its rubric together, and both are recorded with
every run. A run graded by one cannot be compared with a run graded by another, so when either
changes, the run to compare against is graded again first, under a label of its own:

```bash
uv run python -m portfolio_ai.evals rejudge luna-v3 --label luna-v3-regraded --dry-run
uv run python -m portfolio_ai.evals rejudge luna-v3 --label luna-v3-regraded
```

That is how `v2-baseline-sol` was made from `v2-baseline` when the judge became `gpt-6.1-sol`.

`rejudge` has the current judge grade a stored run's answers, and stores the result as a new
run. Nothing is asked again: the answer, the route, what was retrieved, the rule checks, the
timings and what the answer cost are copied from the source run, and only the judge's scores,
its rationale and its call are new. The two findings that rest on whether an answer declined
are worked out again, because that is the judge's reading.

The new run's configuration is the source run's with the judge replaced. It carries
`rejudged_from`, and `show` says whose answers a re-grade holds. `git` stays the commit that
wrote the answers, and the commit that graded them again is `rejudged_git`. The first two
re-grades, `sol-try` and `v2-baseline-sol`, were made before that distinction: their `code` line
is the commit they were graded at, and the answers' is `v2-baseline`'s.

It refuses when the answers cannot be graded fairly, and it refuses before a run is created:
the source run did not complete, the dataset file has changed, or the passages the answers
were shown can no longer be read back. A stored result keeps only the ids of those passages,
and two things can come between an id and its text:
- a document that has changed since. The knowledge base's fingerprint is of what the documents
  say, and is then no longer the one the source run recorded;
- a document indexed again without changing, which is what `ingestion --force` does. Every
  chunk gets a new id and the fingerprint stays what it was, so the ids are looked up as well.

`compare` knows about this. Given two runs graded by different judges, it says so first and
leaves out everything that is the judge's reading: its three scores, which answers declined,
the count of cases breaking a rule (two of the rules are those findings), and a judge's own
failure to grade an answer. Routing, retrieval, the other rules, speed and cost are still
compared.

### Operational

Every result keeps:
- latency, and time to the first word;
- tokens and the answer's cost;
- `calls`: one record per OpenAI call behind it, the judge's included, with its step, model,
  tokens (reasoning tokens apart), latency and cost. It is the same record `chat_messages`
  keeps in `llm_calls`.

The judge's cost is kept apart. The totals report the median and 95th percentile of first-word
and whole-answer time for knowledge-base answers, and the mean reasoning tokens of the chat's own
calls. Those are the numbers the reasoning-effort decision turns on. Reasoning tokens are billed
and never shown, and they are what a lower effort saves. The judge, the embeddings and the
classifier are left out of that mean: the judge thinks on the grader's bill, embeddings do not
reason, and the classifier has an effort setting of its own. Runs stored before the 2026-09-25 fix
counted the classifier too, so the figure they stored is higher than the chat's.

## The commands

All run from the repository root, locally. `run` and `rejudge` refuse
`ENVIRONMENT=production`: they spend money and write to the eval tables, and none of that
belongs in production.

```bash
uv run python -m portfolio_ai.evals check datasets/golden_v2.yaml
uv run python -m portfolio_ai.evals run --label luna-mini --dry-run
uv run python -m portfolio_ai.evals run --label luna-mini
uv run python -m portfolio_ai.evals run --label top-k-8 --top-k 8
uv run python -m portfolio_ai.evals list
uv run python -m portfolio_ai.evals show top-k-8 --failures
uv run python -m portfolio_ai.evals compare luna-mini top-k-8 --markdown ../compare.md
uv run python -m portfolio_ai.evals rejudge luna-mini --label luna-mini-regraded --dry-run
```

A label is used once, and a comparison means something only when one setting differs. So the
example runs the settings in use first, as `luna-mini`, and then one change against that,
`top_k` 8. The first run is needed because no stored run has exactly those settings: `luna-v3`
had `gpt-6-luna` routing, and the routing has since gone back to `gpt-5-mini`. Comparing
`top-k-8` with `luna-v3` would measure two changes at once. Each of the two runs costs about
$0.55.

`run` uses golden_v2 unless `--dataset` names another, and golden_v1 is named only to add to the
runs already made of it. `rejudge` is for the day the judge changes ("The judge", above).

`compare` refuses two runs of different datasets, because their cases differ. `--markdown`
writes the comparison as a table, ready to paste into the Results section below. The path above
puts it outside the checkout, so it cannot be committed by accident.

`run` takes one option per setting it can vary. Each defaults to what `.env` says:
- `--chat-model` and `--classifier-model`
- `--chat-effort` and `--classifier-effort`, where `default` sends no effort at all, so the
  model's own default applies. `none` is a value in its own right, the lowest a GPT-6 model
  takes, as `minimal` is for `gpt-5-mini`
- `--top-k` and `--max-search-rounds`
- `--judge-model`, or `--no-judge` to skip grading

`--only KEY` runs single cases, and `--concurrency` sets how many run at once (default 4).

**Always read the dry run's configuration before a real run.** The code's defaults are what
production is meant to run: `gpt-6-luna` at `none` writing the answers, `gpt-5-mini` at `minimal`
routing. A development `.env` can say something else, and an option left out of the command is
inherited from it. So a run without options measures whatever `.env` holds and is labelled as if
it were production, and a run that names one setting can differ from its baseline in two. Name
every setting the baseline named, and change only the one being measured.

**A model and its effort go together.** `gpt-6-luna`'s lowest effort is `none` and `gpt-5-mini`'s
is `minimal`, and OpenAI refuses each for the other. `--chat-model gpt-5-mini` on its own would
inherit `none`, so the run is refused before it starts; give both. The settings make the same
check when the process starts, for the same pair set in `.env`.

**Cost.** Grading costs about 0.9¢ an answer at `gpt-6.1-sol`; it was 1.4¢ to 1.6¢ at `gpt-5`.
An answer from `gpt-6-luna` costs about 0.05¢, where `gpt-5-mini` cost 0.2¢ at `minimal`. A full
golden_v2 run is about $0.55, $0.51 of it the judge. `--no-judge` keeps every deterministic
measurement and costs only the answers, about 3¢.

## Which piece does what

| Module | Role |
|---|---|
| `evals/datasets.py` | the YAML format as pydantic models, its validation, and the content hash that freezes a dataset |
| `evals/metrics.py` | retrieval scores and the rules: everything deterministic |
| `evals/judge.py` | the judge's brief, its structured verdict, and what counts as unusable |
| `evals/runner.py` | `prepare` (the checks) and `execute` (the run), with bounded concurrency; `prepare_rejudge` and `rejudge`, the same pair for grading a stored run again |
| `evals/report.py` | a run's totals, and every table and comparison the commands print |
| `evals/cli.py` | `check`, `run`, `rejudge`, `list`, `show`, `compare` |
| `db/evals.py` | the eval tables: dataset sync with the freeze, the run lifecycle, results (as a report reads them, and as they were written), the corpus fingerprint |
| `db/documents.py` | `chunks_by_id`: the passages an answer was shown, read back for the judge |
| `assistant/prompts/judge.md` | the rubric, versioned and pinned like every prompt |
| `migrations/versions/0005_eval_harness.py` | case keys, history, expected fallback, accepted routes, unique labels, and the per-result timing and signal columns |

## Python worth knowing here

**`asyncio.TaskGroup` with a `Semaphore`.** A `TaskGroup` (Python 3.11) is structured
concurrency: every task started inside `async with asyncio.TaskGroup() as group:` has finished
by the time the block ends. If one raises, the others are cancelled and the error comes out of
the block. The `Semaphore(n)` inside each task is a counter that lets at most `n` of them into
`async with gate:` at once and parks the rest. Together they give bounded parallelism with one
exit.

**`ExceptionGroup`.** A `TaskGroup` reports what its tasks raised as an `ExceptionGroup`, since
several can fail at once. `runner.execute` looks inside it. When the error is one of this
project's, with a message written for a person, it raises that one error instead of the group.

**`statistics.quantiles`.** `statistics.quantiles(values, n=20, method="inclusive")` returns the
nineteen cut points between twenty equal groups, so index 18 is the 95th percentile. It needs at
least two values, which `report._percentiles` handles.

**Pydantic on a YAML file.** A YAML loader gives dictionaries and lists that accept anything.
`Dataset.model_validate` turns them into typed, frozen objects or fails with the location of
every problem (`cases.12.category`). Tuples rather than lists, together with `frozen=True`,
mean nothing can change a case halfway through a run.

**A loader subclass for the repeated key.** `datasets._StrictLoader` is `yaml.SafeLoader` with
one constructor replaced: the one that builds a mapping, which now refuses a key it has already
seen. Subclassing keeps the change local. Registering the constructor on `SafeLoader` itself
would change `yaml.safe_load` for every caller in the process, the prompt loader's included.

**What the content hash leaves out.** `model_dump(mode="json", exclude_defaults=True)` dumps
only the fields that differ from their defaults. Without it, the hash would depend on the code as
well as the file: add one optional field to `EvalCase` and every dump gains `"new_field": null`,
so every frozen dataset would read as changed and be refused.

**`TYPE_CHECKING`.** `runner.py` needs `Decimal` and `RetrievedChunk` only in annotations on
local variables. Python never evaluates those annotations, so the imports sit under
`if TYPE_CHECKING:`, which is `False` when the program runs and `True` while mypy reads the file.
Annotations on a function's parameters *are* evaluated in Python 3.12, which is why imports used
there cannot move.

**`\N{...}` escapes.** `"\N{RIGHT SINGLE QUOTATION MARK}"` is the curly apostrophe, ’, by
name. In source code it cannot be mistaken for a straight apostrophe, and the `re` module
understands the same syntax inside patterns (`[-*\N{BULLET}]`).

**`\b` and the apostrophe.** `\b` is a word boundary: a point between a word character (letter,
digit, underscore) and anything else. An apostrophe is not a word character, so
`I'm Mihail\b` matches inside "I'm Mihail's assistant", the one answer to "Are you Mihail?" that
is exactly right. The `spoke_as_mihail` pattern adds `(?!')`, a negative lookahead: match only
when the next character is not an apostrophe.

**`subprocess.run` for the git commit.** `capture_output=True, text=True, check=True` keeps the
output, decodes it, and turns a failure into an exception. `shutil.which("git")` resolves the
full path first. On Linux and macOS that means a `git` planted in the working directory is not
the one that runs. Windows searches the current directory first unless the
`NoDefaultCurrentDirectoryInExePath` environment variable is set. It is not set by default, so
on Windows the protection is weaker.

## How it is tested

- **`tests/unit/test_eval_datasets.py`** covers the validation rules one by one, a repeated YAML
  key, and the content hash: field order, the description and a default written out do not
  change it, and a case does. It also checks every real golden file, the frozen golden_v1
  included: that it is valid, and that its prompt-injection case would catch either answering
  prompt leaking. Every YAML file in `datasets/` is checked for email addresses other than
  Mihail's two published ones.
- **`tests/unit/test_eval_metrics.py`** covers the retrieval arithmetic, and every rule both
  firing and not firing. For links that means allowed links from the prompt, a document's URL, a
  passage, a relative link resolved against the site, and the same page written with `www.`,
  bold or a `mailto:`. For persona it means "I'm Mihail's assistant", straight and curly. A
  required phrase is found through the non-breaking hyphens and spaces a model writes.
- **`tests/unit/test_eval_judge.py`** runs the judge through the fake OpenAI, so the real SDK
  builds the request and parses the reply. It checks what the judge is shown, that unusable and
  out-of-range replies are recorded rather than raised, that null scores are allowed, and that
  its longer timeout reaches the request without changing any other call's.
- **`tests/unit/test_eval_runner.py`** replaces the assistant, the judge and the database with
  stand-ins, and checks:
  - an unpriced model, an effort the model does not take, a used label, a frozen dataset that
    changed and an unindexed document are each refused before anything runs, and the plan says
    where the dataset stands;
  - every case is stored with its measurements and its calls, and the fixed reply goes
    ungraded;
  - a failing case is recorded while the rest continue;
  - a rejected key stops the run and marks it failed;
  - concurrency stays within its bound;
  - a cancelled run is marked failed;
  - a stored run graded again keeps each answer and what was measured of it, and replaces the
    judge's scores, rationale and call; the two findings that rest on the judge's reading are
    worked out again; the new run names its judge, the run its answers came from, and the
    commit that wrote them apart from the commit that graded them; nothing of the first judge
    is left on an answer the new one fails on, and an error the first judge left goes when the
    new one grades;
  - a re-grade is refused before anything is spent when the source run is missing or
    incomplete, the label is taken, the judge has no price, the dataset or the knowledge base
    has changed, the passages have new ids, or a case asked for has no result; and a passage
    that goes while the answers are being graded stops it.
- **`tests/unit/test_eval_report.py`** checks the totals: accepted routes, averages that skip
  what was not applicable, the phrase-match agreement, percentiles, reasoning tokens without the
  judge's, and Decimal costs. It also checks the comparison's worse and better lists, both
  table layouts, and that two runs graded by different judges are compared without what the
  judge said: its scores, its findings, the count that includes them and its own failures. And
  that a re-grade is described as one.
- **`tests/unit/test_eval_cli.py`** checks that production is refused, that a dry run executes
  nothing and says where the dataset stands, that an effort option overrides `.env`, that
  `run` defaults to the newest golden file, and that the `check` command printed in the README,
  CLAUDE.md and this guide names that same file. For `rejudge` it checks the production refusal
  and that a dry run grades nothing, and for `compare` that two judges are named and that
  neither the case list nor the Markdown file carries a score. It
  also checks that a console unable to encode a character prints `?` rather than stopping.
  Windows consoles use a code page such as cp1251, and a non-breaking hyphen in a real answer
  crashed the first `show`.
- **`tests/integration/test_eval_db.py`** runs against Postgres:
  - storing a dataset, and replacing it before a complete run;
  - the freeze after one, and no freeze after a run that failed;
  - what the dry run reads, unique labels, and a run's results and calls read back exactly;
  - a result read back equal, field by field, to the one that was written.
- **`tests/integration/test_retrieval.py`** checks that the passages an answer was shown come
  back by id in the order asked, with a missing id simply absent. It asks for one id twice:
  Postgres can return `id = any(array)` rows in the array's order by itself, and never returns
  a row twice, so only the repeat proves the ordering is this code's.
- **`tests/integration/test_eval_run.py`** runs a whole three-case run with only OpenAI faked,
  from the classifier's call to the stored totals. Then it grades that run again: only the
  judge is called, it is shown the passage read back by id, and the answers are the first
  run's. And it indexes the same document again, with the same text, and checks the re-grade
  is refused with no second run created.

## Results

### Reasoning effort, 2026-09-25

Three complete runs of golden_v1 against the same corpus (11 documents, 117 chunks, fingerprint
`a7481f75166f`). The only setting that changed was the chat model's reasoning effort. The
classifier stayed at its default in all three, and so did `gpt-5-mini`, `top_k` 20 and up to three
searches. `baseline` is n8n's configuration, which makes it parity by construction.

| | baseline (default) | effort-low | effort-minimal |
|---|---|---|---|
| first word, median / p95 | 15.6s / 22.4s | 5.8s / 9.3s | **3.9s / 5.9s** |
| whole answer, median / p95 | 16.9s / 23.9s | 6.9s / 10.4s | **4.9s / 8.2s** |
| the chat's reasoning tokens per answer | 741 | 147 | 0 |
| cost per answer | $0.0031 | $0.0020 | **$0.0017** |
| faithfulness, 1-5 | 4.86 | 4.78 | 4.62 |
| completeness, 1-5 | 4.58 | 4.26 | 4.60 |
| style, 1-5 | 4.91 | 4.74 | 4.84 |
| misrouted by the classifier | 4 | 7 | 6 |
| retrieval hit rate | 0.892 | 0.811 | 0.838 |
| run cost, answers + judge | $0.75 | $0.72 | $0.74 |

The reasoning-token row counts the chat's calls alone, recomputed from the stored calls. The
totals these runs stored say 692 / 173 / 64, because the report then counted the classifier too.
At `minimal` the chat does not reason at all.

**Read the classifier's row first, because it moves the others.** The classifier ran with the
same settings in all three runs, yet misrouted 4, 7 and 6 cases, and not always the same ones.
That is its own variance, not an effect of the chat's effort. A misrouted question never
searches, so each misroute also counts as a retrieval miss. That is all of the gap in hit rate:
in every run the only misses are the misrouted cases. It is also most of the gap in recall and
MRR, and it is why the retrieval rows are left out of the conclusion.

**On the 36 cases every run sent to the knowledge base, the chat's effort costs almost
nothing.** Over the cases all three runs graded:

| | faithfulness | completeness | style |
|---|---|---|---|
| baseline | 4.96 | 4.68 | 4.94 |
| effort-low | 4.89 | 4.43 | 4.88 |
| effort-minimal | 4.79 | **4.82** | **5.00** |

`minimal` loses a little faithfulness: six 5s become 4s, and none fall lower. It is about as
complete as the baseline. The table can't show one of its failures, pretend-mihail, because the
baseline has no grade for that case. Averaging each run over its own graded cases instead gives
completeness of 4.68, 4.39 and 4.71. `low` is the weakest of the three. Its one clear failure
is `phd-false-premise`, which it answered with "I don't have that information" instead of
correcting the premise. `minimal`'s one clear failure is `pretend-mihail`: it refused the first
person, as it should, but offered the third-person summary instead of giving it. Each is one
case in one run, which is within what a second run of the same settings would move.

**Recommendation: `CHAT_REASONING_EFFORT=minimal`.** The first word arrives in about 4 seconds
instead of 16, and 6 instead of 22 at the 95th percentile. The answer costs 45% less, with no
measurable loss in completeness or style and a small one in faithfulness. `low` is slower than
`minimal` and scored no better. The decision is the owner's; the numbers are one run each, on 54
cases.

**What the runs found besides effort.**
- **The classifier misroutes project questions**, and this is the biggest quality problem the
  evals found:
  - Five questions were sent to `out_of_scope` in one run or more, and got the fixed refusal:
    - "What is the n8n Pro Automation Framework?"
    - "How does this chat assistant work?"
    - "Is Glotsmith an AI chatbot?"
    - "Why did Glotsmith switch from Stripe to Paddle?"
    - "How does moderation work in Threadline?"
  - "yes please" and "sure", following Rachel's own offer, went to `small_talk` in most runs.
    That happened even with the previous exchange given.

  Its prompt was n8n's, ported verbatim. The fix is below, under "The classifier's prompt".
- **The CV link:** the baseline and `low` pointed to the contact section rather than giving
  `Mihail_Mihaylov_CV.pdf`, which the knowledge base states.
- **The judge's timeout:** the baseline lost three verdicts (marketing-reporter,
  pretend-mihail, everything) to the 30-second `OPENAI_TIMEOUT_SECONDS`. That limit is sized for
  a visitor, not for `gpt-5` reading twenty passages. The judge now has 180 seconds of its own
  (`JUDGE_TIMEOUT_SECONDS`), and the later runs graded every case. The baseline's averages cover
  those three cases fewer, which is one more reason the matched-case table is the one to read.
  OpenAI may have billed those timed-out attempts. Nothing comes back to record, so the
  baseline's judge total of $0.58 probably understates what it cost.

### The classifier's prompt, 2026-09-25

Production switched both efforts to `minimal` after the runs above, and the classifier at
`minimal` had never been measured. So the "before" here is the classifier alone, at production's
settings. A throwaway script sent every golden_v1 question to it five times, with no search, no
answer and no judge, which gives 270 routing decisions per prompt for about 6¢.

| classifier at `gpt-5-mini` / `minimal` | wrong, of 270 | cost |
|---|---|---|
| prompt version 1 (n8n's) | 28 | $0.055 |
| prompt version 2 | **0** | $0.068 |

**Version 1 at `minimal` was worse than at the default effort.** "What is Glotsmith?" went to
`out_of_scope` five times out of five, and so did four other questions about his projects. At the
default effort the same prompt got "What is Glotsmith?" right in all three runs. With less
reasoning, the model stopped inferring that an unfamiliar product name on this site is probably
his. `glotsmith-paddle` went there once in five and "who are you?" once. The three other project
questions, about Threadline's stack, the Marketing Reporter and the projects page, were right
every time: five of eight project questions failed. "sure" went to small talk once in five, and
"yes please" never did.

**The cause was the same for all of them: the classifier sees one message, not the knowledge
base.** A question that names a project and not Mihail looks like a question about some unknown
product, and a bare "yes please" looks like small talk unless the prompt says to read it against
the previous turn. Version 2 adds three things to n8n's text, and removes nothing:
- the names of his projects, with the rule that a question about one is about his work even when
  it does not name him;
- a rule that a short reply to the assistant is classified by what it accepts;
- three examples.

The examples are deliberately not golden_v1's questions ("What database does Threadline use?",
"Who built this chat?"), so the fix isn't tuned to the test. The out-of-scope questions (the
capital city, SQL help, the prompt injection) went to `out_of_scope` all 30 times: naming the
projects did not pull unrelated questions in.

The list of names goes stale the day a project is added, so `portfolio-ai-validate` warns about
any `page_type: project` article whose title words are not all in the prompt. It runs in the
portfolio repository's CI.

**The full run at production's settings** (`classifier-v2`: chat and classifier at `minimal`,
prompt version 2), compared with `effort-minimal`:

| | effort-minimal | classifier-v2 |
|---|---|---|
| misrouted | 6 | **0** |
| classification | 0.889 | **1.000** |
| retrieval hit rate | 0.838 | **1.000** |
| MRR | 0.806 | 0.946 |
| faithfulness / completeness / style | 4.62 / 4.60 / 4.84 | 4.93 / 4.49 / 4.71 |
| first word, median / p95 | 3.9s / 5.9s | 3.4s / 4.7s |
| run cost | $0.74 | $0.82 |

The two runs differ in classifier effort as well as in the prompt, and the classifier-only check
separates the two only partly:
- **The project questions:** it does separate them. Version 1 at `minimal` got the four in this
  table wrong every time, so the prompt is what fixed them.
- **The follow-ups:** it doesn't. Version 1 at `minimal` already routed "yes please" 5/5 and
  "sure" 4/5. Their failures in `effort-minimal` came from the classifier at the default effort.
  So the short-reply rule is unmeasured at the effort production runs. It rests on the misroutes
  at default effort and on its reasoning.

That rule also quotes "yes please" and "sure", which are golden_v1's own follow-ups. The next
version of the prompt should quote replies the dataset doesn't use, and it should say "the
previous exchange", which is what the classifier is actually given, rather than "the
conversation so far". Both are wording changes, so they wait for a version 3 and a run of their
own.

Six cases scored lower, and none of them has anything to do with routing: each went to the
knowledge base in both runs. They are the chat at `minimal` being variable:
- `pretend-mihail` failed at `minimal` in both runs. It declines the first person correctly, then
  offers the third-person summary instead of giving it, and this time introduced itself.
- `projects-page-link` answered with a bulleted list.
- `phd-false-premise`, and the two questions it cannot answer (`married`, `football-team`),
  scored 3 for style instead of 5.
- `postgres-projects` scored 3 for completeness instead of 5.

Those are the chat prompt's to fix, and the next thing worth measuring.

Total spend on this change: $0.94.

### golden_v2's baseline, 2026-10-09

The first run of golden_v2 (`v2-baseline`), at the settings production runs: chat and classifier
at `gpt-5-mini` / `minimal`, classifier prompt version 2, `top_k` 20 and up to three searches,
against the knowledge base as it reads after the cutover (11 documents, 122 chunks, fingerprint
`ff5bf7fa2152`). It is not comparable with any run above, which were all of golden_v1. Since the
judge changed on 2026-10-10, later runs are compared with `v2-baseline-sol`, which is these same
answers graded by the new judge ("A new judge", below). The run records its code as
`f034a6c485f7` with uncommitted changes: those were this dataset, the new default and their
tests, none of which changes how a question is answered or graded.

| | v2-baseline |
|---|---|
| cases | 62 answered, none failed, no verdict lost |
| classification | 1.000 |
| retrieval hit rate / recall / MRR | 1.000 / 0.958 / 0.931 |
| precision | 0.418 |
| faithfulness / completeness / style | 4.92 / 4.60 / 4.71 |
| first word, median / p95 | 2.8s / 6.3s |
| whole answer, median / p95 | 3.6s / 7.1s |
| cost per answer | $0.0019 |
| run cost, answers + judge | $0.12 + $0.89 = $1.01 |

**The 14 changed and new cases all reached the knowledge base, found an expected article, and
scored 5 for faithfulness.** Ten scored 5 on all three. What the cutover reversed held: no answer
put Python outside his stack, none called the assistant an n8n system, and `python-projects` named
the assistant alone. `assistant-source` gave the repository's address whole, so the link filter
leaves a real address alone.

The four that lost points:
- **`assistant-how`** (completeness 4, style 3) and **`assistant-built`** (style 3) were right and
  too long: five sentences or more, where the prompt asks for two to four. `assistant-how` also
  left out the three search rounds and the sources listed under an answer.
- **`n8n-work`** (completeness 4) named the framework and the Marketing Reporter, and left out
  both the Businessmap automation and the assistant's first version. Retrieval had not brought
  the assistant's article or `tech-stack.md` (recall 0.67).
- **`assistant-privacy`** (completeness 4) left out that the chat sets no cookies.

**The judge missed an invention in two answers.** `assistant-privacy` ended "If you want to read
the full policy or contact Mihail, see https://mihaylov.io/#contact". The link is real and the
policy is not: the site has none of its own, and the only privacy link in its contact section
is Google's, for reCAPTCHA. `assistant-how` said conversations are stored "with privacy measures
described on the site", and the site describes only the 90 days and the request not to share
personal details; the rest is in the article, which is not a page. A similar question, typed
into the terminal chat with `--no-save` to check the new articles before cutover, got a pointer
to a "privacy section". The judge scored faithfulness 5 both times: everything the answers say
about the chat is in the passages, and it did not count the pointers. No phrase rule can catch
them either, since the wording changes each time. It is the chat prompt's to fix, and until
then a reason to read these answers and not only their scores.

**`assistant-quality` repeated the article's "54 questions"**, which is golden_v1's size. The
reference leaves the number out, so the answer scored 5. The article is what would change.

**Four answers open with "Short answer:" or "Briefly:"**, all of them to questions about the
assistant (`assistant-how`, `assistant-built`, `assistant-n8n`, `assistant-quality`). No rule
forbids it, and it is not how Rachel is asked to talk.

**Six carried-over cases that were weak in `classifier-v2`, the same settings on golden_v1, are
still weak**, all of them the chat at `minimal`:
- `pretend-mihail` (completeness 1) declines the first person and offers the third-person summary
  instead of giving it, as in both earlier runs at `minimal`.
- `everything` (style 1, was 3) ran to 32 sentences and 31 bullet lines.
- `projects-page-link` (faithfulness 4, completeness 3) and `followup-sure` (completeness 3).
- The fallback answers `married` and `football-team` scored 3 for style.

**Three are new in this run**, and one run does not say whether they will stay:
`postgres-projects` used a three-item bullet list (and scored 5 on everything else, where it had
scored 3 for completeness), and `hourly-rate` and `n8n-framework` scored 3 for style where they
had scored 5.

**Retrieval:** five cases missed an expected article, and in four of them it was
`tech-stack.md` (`businessmap-delivery`, `n8n-work`, `assistant-n8n`, `assistant-quality`). Every
case still found one that answers it. Two of those four are less a retrieval finding than a
listing one: `assistant-n8n` and `assistant-quality` are new cases that list `tech-stack.md` on
the strength of a single clause. Precision of 0.418 is `top_k` 20 on a corpus of 122 chunks:
most of what is retrieved is not needed, which is the case for trying a smaller `top_k` against
this baseline. Expect that run to lower recall on exactly these borderline listings.

**For golden_v3**, since golden_v2 is frozen. The review after the run found these in the cases:
- **One rule for one sentence.** Three articles say only that the assistant is built in Python
  (`faq.md`, `services.md`, the assistant's own). `python-projects` lists all three, `python` and
  `followup-and-python` list one, `assistant-n8n` lists them for a question about n8n, and
  `assistant-quality` rules the same `services.md` bullet out as a passing mention. `n8n-work`,
  unchanged from golden_v1, lists `faq.md` and not `services.md` for the same kind of mention.
- **`assistant-privacy`'s reference** says the conversation "is also kept in the visitor's own
  browser for 24 hours". The article and the site's code say it can be resumed for 24 hours,
  which is weaker: a copy nobody returns to stays where it is.
- **`assistant-how`'s reference** says "the articles it drew on" where the article says the ones
  "its searches found most relevant".
- **`n8n-work`'s note** has `faq.md` naming the Marketing Reporter among his n8n work. It names
  the project and does not say it runs on n8n.
- **"90 days" as a hard phrase** fails a correct "90-day retention". It is better left to the
  judge.

The same review found that a hard phrase could be missed in an answer that has it: models write
"gpt-5-mini" and "90 days" with non-breaking hyphens and spaces, and five of this run's answers
held such a character. None cost a case here. `metrics._plain` now folds them on both sides of
the match.

Total spend on this change: $1.01. The estimate before the run was $0.90: grading cost 1.6¢ an
answer here, not the 1.4¢ measured on golden_v1.

### The chat prompt, version 2, 2026-10-10

golden_v2's baseline left four faults in Rachel's answers: answers that run long, parts of an
answer labelled ("Short answer:"), a refusal to speak as Mihail that offers a summary instead of
giving it, and pointers to pages the site does not have. The chat prompt was n8n's, word for
word.

Each wording was measured with repetitions before any full run: the affected cases answered
five times each through `agent.respond()`, at production's settings, with no judge and nothing
stored. It is a throwaway script outside the repository, as the classifier fix used. Eight
cases at first, with `cv-download` and `assistant-source` added as controls once a rewording
touched the rule about links.

Three wordings on `gpt-5-mini` at `minimal`, against version 1:

| | version 1 | five lines added | eight lines reworded | three reworded, one added |
|---|---|---|---|---|
| `pretend-mihail` gives the summary | 0 of 5 | 5 of 5 | 5 of 5 | 5 of 5 |
| `assistant-how` within four sentences | 2 of 5 | 0 of 5 | 5 of 5 | 5 of 5 |
| `everything` within four sentences | 0 of 15 | 0 of 5, and 1 fallback | 0 of 5, and 1 fallback | 0 of 5 |
| bullet lists | 5 of 40 | 5 of 40 | 1 of 40 | 1 of 40 |
| labels ("Short answer:") | 7 of 40 | 2 of 40 | 5 of 40 | 4 of 40 |
| privacy answer points to a note the site lacks | 5 of 5 | 4 of 5 | 2 of 5 | 3 of 5 |
| "Yes" opening a how or what question | 0 of 20 | 0 of 20 | 13 of 20 | 0 of 20 |
| repository link given (control) | 4 of 5 | not run | 2 of 5 | 4 of 5 |

- **Adding lines beside n8n's did not outweigh them.** The one addition that worked everywhere
  is the identity line: asked to speak as Mihail, decline and give the answer in the third
  person in the same reply.
- **Rewording more of the prompt fixed one fault and caused another.** A sentence saying when an
  answer may open with "Yes" or "No" put "Yes" at the start of thirteen of twenty answers to how
  and what questions, and of all five to "What is the n8n Pro Automation Framework?". A reworded
  rule 9 halved how often the repository link was given. Both
  rewordings were dropped, and rule 9 and the "Answer pattern" went back to n8n's text.
- **A sentence about requests for "everything" was dropped too.** With some form of it, "tell me
  everything" fell back to "I don't have that information" three times in fifteen; without it,
  never in twenty-five, and the answers were no longer.

Two lessons, both about a small model at the lowest effort. Naming a phrase to avoid can bring
it out, and a rule aimed at one case leaks into others. And several lines changed at once
cannot be told apart afterwards: the last attempt was the smallest for that reason.

**The closing offer was found on gpt-6-luna** ("gpt-6-luna", below). n8n's step 3 of the answer
pattern begins "Optionally", and Luna took it at its word: one offer of more detail in 51
answers, where `gpt-5-mini` made one in 33 of 52. Making step 3 firm got 18 of 50. Saying it in
the sentence rule as well, which Luna obeys, got 39 of 50.

Version 2 as committed adds one line and replaces four of n8n's:

- added, under the identity rules: asked to speak as Mihail, say briefly that you cannot and
  give the answer in the third person in the same reply, in the usual 2-4 sentences;
- "Default answers should be 2-4 sentences." is now a rule with both ends: never one sentence
  alone, never more than four unless more detail was asked for on one topic, and the last
  sentence offers to say more;
- "Do not write long structured answers unless the user asks for more detail." is "Do not write
  long or structured answers.";
- "Prefer short paragraphs over bullet lists." is "Use a bullet list only when the user asks
  for a list.";
- step 3 of the answer pattern, "Optionally, one short sentence offering more details…", is
  "Always finish with one short sentence that offers to say more about a specific part of the
  topic", except for the fallback sentence.

`tests/unit/test_prompts.py` lists the four replaced lines and holds the prompt to every other
line of n8n's, in order.

Not fixed by wording, on either model: "tell me everything" still runs to nine sentences or
more. The repetitions cost $0.55 in all.

### A new judge, 2026-10-10

The judge changed twice over in one step: the model from `gpt-5` to `gpt-6.1-sol`, and the
rubric from version 1 to version 2 ("The judge", above, has the two rubric fixes and why). So
that there would be something to compare with, `rejudge` had the new judge grade the answers
`v2-baseline` had stored, as `v2-baseline-sol`. The 62 answers, their routes, what was
retrieved and what they cost are identical in the two runs.

| | `gpt-5`, rubric 1 | `gpt-6.1-sol`, rubric 2 |
|---|---|---|
| faithfulness | 4.92 | 4.82 |
| completeness | 4.60 | 4.19 |
| style | 4.71 | 4.39 |
| cases breaking a rule | 3 | 2 |
| judge's cost for the run | $0.89 | $0.51 |

- **It is a stricter reader.** Completeness is lower on 15 answers, eleven by one point and four
  by two, and in 14 of them its reason is a fact of the reference left out. Style is lower by
  two on 12; the reasons read were a missing offer of more detail and a sentence that "reads
  like a CV inventory".
- **Both rubric fixes bite.** `assistant-privacy`'s "read the full policy" now costs it a
  faithfulness point ("implies a site document that the passages do not establish"), and
  `projects-page-link`'s "Threadline project page" takes that answer from 4 to 2. The three
  one-sentence fallback answers score 5 for style, where they scored 3.
- **One invented pointer still passes:** `assistant-how`'s "privacy measures described on the
  site" kept its 5. The rule is in the rubric; this judge did not apply it to that sentence.
- **`pretend-mihail` is no longer read as declining.** It refuses the first person and offers
  the summary, which the first judge counted as declining an answerable question. Completeness
  stays at 1 either way.
- **It costs less**, because it reasons less: a median of about 4,000 tokens read and 160
  written per answer, where `gpt-5` wrote about a thousand, most of them reasoning.

Nothing graded by the first judge is comparable with anything graded by the second. `compare`
says so and leaves the scores out when asked to try.

### gpt-6-luna, 2026-10-10

`gpt-6-luna` is OpenAI's smallest GPT-6 model, released in September 2026, at $0.10 / $0.01 /
$0.50 per million input, cached and output tokens, against `gpt-5-mini`'s $0.25 / $0.025 / $2.00.
Its lowest effort is `none`; it does not take `minimal`.

**Repetitions first** (the ten cases, five times each, `gpt-5-mini` still classifying). At
`none` with the version 2 prompt as it then stood, Luna gave the `pretend-mihail` summary within
four sentences five times of five, wrote no labels and no bullet lists in forty answers, and
never pointed the privacy answer at a note the site lacks. Effort `low` bought nothing and was
slower. On the version 1 prompt, two of five `pretend-mihail` answers were a first-person draft
in Mihail's voice, so Luna needs version 2 as much as `gpt-5-mini` did.

**Three full runs**, all of golden_v2, all graded by `gpt-6.1-sol` with rubric 2:

| | v2-baseline-sol | luna-v2 | luna-v3 |
|---|---|---|---|
| chat | `gpt-5-mini`, `minimal` | `gpt-6-luna`, `none` | `gpt-6-luna`, `none` |
| classifier | `gpt-5-mini`, `minimal` | `gpt-5-mini`, `minimal` | `gpt-6-luna`, `none` |
| chat prompt | version 1 | version 2 before the closing offer | version 2 |
| classification | 1.000 | 1.000 | 1.000 |
| retrieval hit rate / recall / MRR | 1.000 / 0.958 / 0.931 | 1.000 / 0.985 / 0.963 | 1.000 / 0.985 / 0.974 |
| faithfulness | 4.82 | 4.85 | **4.92** |
| completeness | 4.19 | 4.08 | 4.17 |
| style | 4.39 | 3.57 | **4.82** |
| answers closing with an offer | 33 of 52 | 1 of 51 | 37 of 52 |
| first word, median / p95 | **2.8s** / 6.3s | 3.3s / 5.1s | 4.0s / 5.0s |
| whole answer, median / p95 | 3.6s / 7.1s | 4.0s / 5.9s | 4.6s / 6.0s |
| cost per answer | $0.0019 | $0.0006 | **$0.0004** |
| run cost, answers + judge | $0.12 + $0.51 | $0.04 + $0.52 | $0.03 + $0.51 |

- **luna-v2's style fell to 3.57 for one reason.** The judge names the missing offer of more
  detail in 35 of the 40 answers it scored 3. Eleven answers were also a single sentence. The
  prompt change above is the fix, and luna-v3 is the run after it.
- **luna-v3 against the baseline:** seventeen cases better, seven worse. `pretend-mihail` went
  from 1 to 5 on completeness, and there are no bullet lists.
- **One answer is clearly worse.** Asked "Where did Mihail do his PhD?", Luna said the
  knowledge base "doesn't mention a PhD" and that it has no information on where he "may have
  completed one" (completeness 2). `gpt-5-mini` said he does not have one. One case, and in
  both Luna runs: luna-v2 scored 3 on it.
- **One required phrase is missing, as formatting.** `assistant-models` wrote "**GPT-5 mini**"
  where the case requires "gpt-5-mini", in both Luna runs. The judge gave the answer 5 on
  everything.
- **Luna writes Markdown**: bold in 8 of luna-v3's answers and links as `[text](address)`. The
  site's chat renders both.
- **"Tell me everything" is still too long**, at style 3.

**The classifier stays on gpt-5-mini.** Checked on its own, every golden_v2 question several
times with no search and no answer, both models routed everything right: `gpt-6-luna` at `none`
0 wrong of 434, `gpt-5-mini` at `minimal` 0 wrong of 124 in the same sitting. What differs is
time. Every message waits for the classifier before anything else happens, and its call takes
about 0.8s on `gpt-5-mini` and about 1.4s on `gpt-6-luna`, which is most of the gap between
luna-v2's first word and luna-v3's. Luna would save $0.00017 a message.

**Decided:** `gpt-6-luna` at `none` writes the answers, with chat prompt version 2; `gpt-5-mini`
at `minimal` routes; `gpt-6.1-sol` judges. Those are the code's defaults since this change.

**Not measured:** no single run has exactly that pairing with the final prompt. luna-v3 has the
final prompt and Luna classifying; luna-v2 has `gpt-5-mini` classifying and the prompt before
the closing offer. The two runs routed 61 of the 62 cases alike. `who-are-you` went to small
talk in luna-v2 and to the knowledge base in luna-v3, both of which the case accepts, and it is
why luna-v2 has 51 knowledge-base answers and luna-v3 52. So the scores to expect are about
luna-v3's and the first word about luna-v2's 3.3s. A run of the pairing itself, about $0.55,
would be the baseline to measure the next change against.

Both Luna runs record the chat prompt as `rag_agent@2`, and they are two texts: the closing
offer was added between them, while version 2 was still uncommitted, and the number was not
raised. The "chat prompt" row above is what tells them apart; the recorded version does not.

**More for golden_v3**, since the model changing makes two cases describe the past:
- `assistant-models` requires "gpt-5-mini", which is what the knowledge base article says
  writes the answers. When production moves to `gpt-6-luna` the article changes, and this case
  with it. A required phrase with a hyphen in it also fails "GPT-5 mini", which is the same
  model written as a name.
- `assistant-quality`'s reference gives `gpt-5` as the judge and the 15.6s to 3.9s result,
  which is the article's account of the first runs.
- `projects-page-link` scores 2 for faithfulness under rubric 2 in all three runs, including
  luna-v3's answer, which says what the reference says: Threadline is in the projects section,
  with the section's link. The judge reads "in the projects section" as a claim about the site
  that no passage makes. Either the knowledge base says where a project is shown or the
  reference stops saying it; as it stands the case cannot be passed.

Total spend on 2026-10-10, across the prompt, the judge and the model: about $2.40.
