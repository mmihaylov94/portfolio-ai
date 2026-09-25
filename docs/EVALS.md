# The evals, end to end

How the eval harness scores Rachel against a golden dataset, module by module:
- what it measures, and how
- how a run is stored so that it can be compared months later
- how to read a comparison without being fooled by noise

The code is in `src/portfolio_ai/evals/`, the SQL in `db/evals.py`, and the cases in
`datasets/golden_v1.yaml`. ARCHITECTURE.md §9 has the design this implements.

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
  test (`JUDGE_MODEL`), and its rubric is versioned like any prompt. Trying `gpt-5` as the chat
  model changes what is measured, never how.
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

This command runs the baseline, in two phases:

```bash
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label baseline \
    --chat-effort default --classifier-effort default
```

**Prepare** (`runner.prepare`) is every check that can refuse a run, and it spends nothing:
- every model has a price in `llm/pricing.py`;
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

`datasets/golden_v1.yaml` has 54 cases, written from the eleven knowledge-base articles:

| Kind | Cases | What it checks |
|---|---|---|
| One-document questions, and two false premises ("Where did Mihail do his PhD?", "Is Glotsmith an AI chatbot?") | 31 in all | retrieval, completeness, facts |
| Questions spanning documents ("Which projects use PostgreSQL?") | 3 of those 31 | recall across several documents |
| Not in the knowledge base (salary, rates, age) | 5 | the fallback, no guessing |
| Follow-ups with the previous exchange ("yes please", "And Python?") | 4 | the classifier's context |
| Small talk, including "Who are you?" and "Are you Mihail?" | 5 | route, persona |
| Out of scope | 5 | route, the fixed reply |
| Adversarial: prompt injection, "pretend to be Mihail", a `/projects/` link, "tell me everything" | 4 | rules, persona, length |

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

`python -m portfolio_ai.evals check datasets/golden_v1.yaml` validates the file without a
database or network, and a unit test runs the same check in CI. Validation is pydantic, with
`extra="forbid"`: a misspelt field such as `expected_docs` is an error rather than a case that
tests nothing and passes. Rules that span fields are checked in a `model_validator`, for
example:
- only `mihail_related` questions can expect documents;
- a question the knowledge base cannot answer expects no documents;
- history alternates visitor and Rachel, and ends with her answer.

A key written twice in the same mapping is refused too, before pydantic sees the file. Plain
YAML keeps the second of two equal keys without a word, so a case with `expected_doc_ids`
written twice would be scored on the second, and `extra="forbid"` would never see the first.

**To change a dataset after its first complete run**, copy it to `golden_v2.yaml`, rename it
inside, and edit the copy. Until a run completes, edits replace the stored cases freely. That
includes after a run that failed part-way: it has no scores worth protecting, and its partial
results are deleted with the old cases.

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

`evals/judge.py` asks `JUDGE_MODEL` (`gpt-5`) to grade each answer against the rubric in
`prompts/judge.md`. It uses the same `responses.parse` structured call the classifier uses, so
the reply is held to a schema:

| Field | Scale | Against |
|---|---|---|
| `faithfulness` | 1-5, or null | the passages Rachel was shown: nothing claimed that they don't support. Null only when the answer claims nothing about Mihail |
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

All run from the repository root, locally. `run` refuses `ENVIRONMENT=production`: it spends
money and writes to the eval tables, and none of that belongs in production.

```bash
uv run python -m portfolio_ai.evals check datasets/golden_v1.yaml
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label baseline \
    --chat-effort default --classifier-effort default --dry-run
uv run python -m portfolio_ai.evals run --dataset golden_v1 --label baseline \
    --chat-effort default --classifier-effort default
uv run python -m portfolio_ai.evals list
uv run python -m portfolio_ai.evals show baseline --failures
uv run python -m portfolio_ai.evals compare baseline effort-low --markdown ../compare.md
```

`compare` refuses two runs of different datasets, because their cases differ. `--markdown`
writes the comparison as a table, ready to paste into the Results section below. The path above
puts it outside the checkout, so it cannot be committed by accident.

`run` takes one option per setting it can vary. Each defaults to what `.env` says:
- `--chat-model` and `--classifier-model`
- `--chat-effort` and `--classifier-effort`, where `default` sends none, so the model's own
  default applies
- `--top-k` and `--max-search-rounds`
- `--judge-model`, or `--no-judge` to skip grading

`--only KEY` runs single cases, and `--concurrency` sets how many run at once (default 4).

**Always read the dry run's configuration before a real run.** The development `.env` sets both
efforts to `low`. A "baseline" run without `--chat-effort default --classifier-effort default`
would be measuring something other than what production runs, and would be labelled as if it
were. The same goes for the runs compared against it: `--chat-effort low` alone also inherits
the classifier's `low` from `.env`, which changes two settings at once, so pass
`--classifier-effort default` with it to change only the one being measured.

**Cost.** Grading costs about 1.4¢ an answer at `gpt-5`. A knowledge-base answer at `gpt-5-mini`
costs about 0.4¢ at the default effort and 0.2¢ at `minimal`. A full golden_v1 run is about $0.75,
most of it the judge. `--no-judge` costs $0.10-0.17 and keeps every deterministic measurement.

## Which piece does what

| Module | Role |
|---|---|
| `evals/datasets.py` | the YAML format as pydantic models, its validation, and the content hash that freezes a dataset |
| `evals/metrics.py` | retrieval scores and the rules: everything deterministic |
| `evals/judge.py` | the judge's brief, its structured verdict, and what counts as unusable |
| `evals/runner.py` | `prepare` (the checks) and `execute` (the run), with bounded concurrency |
| `evals/report.py` | a run's totals, and every table and comparison the commands print |
| `evals/cli.py` | `check`, `run`, `list`, `show`, `compare` |
| `db/evals.py` | the eval tables: dataset sync with the freeze, the run lifecycle, results, the corpus fingerprint |
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
  change it, and a case does. It also checks the real golden_v1.yaml: that it is valid and has a
  case for every injection the plan named. Every YAML file in `datasets/` is checked for email
  addresses other than Mihail's two published ones.
- **`tests/unit/test_eval_metrics.py`** covers the retrieval arithmetic, and every rule both
  firing and not firing. For links that means allowed links from the prompt, a document's URL, a
  passage, a relative link resolved against the site, and the same page written with `www.`,
  bold or a `mailto:`. For persona it means "I'm Mihail's assistant", straight and curly.
- **`tests/unit/test_eval_judge.py`** runs the judge through the fake OpenAI, so the real SDK
  builds the request and parses the reply. It checks what the judge is shown, that unusable and
  out-of-range replies are recorded rather than raised, that null scores are allowed, and that
  its longer timeout reaches the request without changing any other call's.
- **`tests/unit/test_eval_runner.py`** replaces the assistant, the judge and the database with
  stand-ins, and checks:
  - an unpriced model, a used label, a frozen dataset that changed and an unindexed document
    are each refused before anything runs, and the plan says where the dataset stands;
  - every case is stored with its measurements and its calls, and the fixed reply goes
    ungraded;
  - a failing case is recorded while the rest continue;
  - a rejected key stops the run and marks it failed;
  - concurrency stays within its bound;
  - a cancelled run is marked failed.
- **`tests/unit/test_eval_report.py`** checks the totals: accepted routes, averages that skip
  what was not applicable, the phrase-match agreement, percentiles, reasoning tokens without the
  judge's, and Decimal costs. It also checks the comparison's worse and better lists, and both
  table layouts.
- **`tests/unit/test_eval_cli.py`** checks that production is refused, that a dry run executes
  nothing and says where the dataset stands, and that an effort option overrides `.env`. It
  also checks that a console unable to encode a character prints `?` rather than stopping.
  Windows consoles use a code page such as cp1251, and a non-breaking hyphen in a real answer
  crashed the first `show`.
- **`tests/integration/test_eval_db.py`** runs against Postgres:
  - storing a dataset, and replacing it before a complete run;
  - the freeze after one, and no freeze after a run that failed;
  - what the dry run reads, unique labels, and a run's results and calls read back exactly.
- **`tests/integration/test_eval_run.py`** runs a whole three-case run with only OpenAI faked,
  from the classifier's call to the stored totals.

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
