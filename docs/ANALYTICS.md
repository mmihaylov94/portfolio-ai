# The analytics, end to end

How the project reads its own chat history back, module by module:
- what an answer records about itself, and what a report makes of it
- why one form of the report quotes visitors and the other never does
- how it was built and checked before the site had visitors

The code is in `src/portfolio_ai/analytics/`, the SQL in `db/analytics.py`, and the invented
conversations it was built against in `datasets/traffic/`. ARCHITECTURE.md §10 has the design.

**Built so far:** the report (`analytics report`) and the invented conversations
(`analytics seed`). The nightly retention sweep, `analytics purge`, is older and is described in
docs/API.md, "Retention". **Not built yet:** the weekly email, the content gaps and
`promote-case`. The last section says what is decided about each.

## The idea it rests on

**An answer leaves evidence about itself at the moment it is given, and the analytics only
ever read that evidence back.** Nothing here asks the assistant anything again. Every answer row
in `chat_messages` already carries:

- the route the classifier chose (`classification`)
- the score of the best passage its searches found (`top_score`)
- whether the reply was the "I do not have that information" answer (`fallback_used`)
- the searches themselves, each with the passages it returned, best first: their chunk ids,
  and beside each the `doc_id` of its article and the slug of its section (`tool_calls`)
- what it cost and how long the visitor waited (`cost_usd`, `first_token_ms`, `latency_ms`)

and a visitor may have left a thumbs up or down on it (`message_feedback`). Those columns shipped
with the API, months before anything read them, because none of them can be worked out
afterwards. This step is the reading.

Three things follow from that sentence, and they explain most of the code.

- **A signal is a list for a person, not a verdict.** A low score does not mean a bad answer.
  A short question such as "Who is Mihail anyway?" scores 0.23 and is answered perfectly well.
  So the report prints the questions behind each count, and the thresholds that draw the lists
  are settings, printed with every report.
- **Visitors' words stay on the database's side.** A report has two forms. One quotes what
  visitors typed, and is printed to a terminal for the site's owner to read. The other gives the
  same counts and not one word a visitor wrote, and it is the only form that may be emailed or
  logged. The chat tells visitors their conversations are kept for 90 days; a question quoted
  into a mailbox would outlive that.
- **What is derived is thrown away.** The one thing the report computes that was not recorded
  is how alike two questions are, which needs their embeddings. They are made when the report is
  built and dropped when it ends. A table of them would be one more copy of visitors' words to
  keep in step with the purge, to save a fraction of a cent.

## One report, start to finish

`uv run python -m portfolio_ai.analytics report --since 7d`

1. **The window** (`report.window`). `7d` becomes two timestamps: seven days ago and now, in
   UTC. A window runs from its start up to, and not including, its end, so two windows that
   share an edge never count one question twice.
2. **The turns** (`db/analytics.turns_between`). One query returns every answered question
   asked in the window: the question row joined to its answer through `reply_to_id`, with the
   feedback left-joined so that the answers nobody rated are still there. The window is measured
   on the question's time. Most of what the report says is then arithmetic over this one list.
3. **The window before** (`db/analytics.volume_between`). Two counts, for "the 7 days before".
4. **The articles** (`db/analytics.article_usage`). For each article, how many answers had one
   of its sections among the five best passages of their first search. See "The signals" for why
   it is the best five and not everything retrieved.
5. **The embeddings** (`llm/embeddings.embed_texts`). Every distinct question that was not small
   talk, each embedded once however many visitors asked it. They are sent
   `EMBEDDING_BATCH_SIZE` at a time, 64 by default, so a quiet week is one request and three
   busy months are several. A week of portfolio traffic is a few hundred tokens.
6. **Questions asked again** (`signals.consecutive_pairs`). Each question is paired with the one
   before it in the same conversation, and the cosine similarity of their embeddings is
   computed. Every pair is kept, with its similarity. The report draws its "asked again" list
   from the ones at or over `REPHRASE_SIMILARITY`, and prints the whole spread at the end.
7. **Questions refused as off-topic** (`db/documents.search_chunks`, `top_k=1`). Each is looked
   up against the knowledge base with the embedding it already has. A refused question that sits
   close to a passage was probably about Mihail, and was turned away by the classifier.
8. **The text** (`report.render`). `verbatim=True` for the terminal.

`build` logs one line, `report_built`, with counts and the embedding cost. Never a question.
The command runs at warning level, which drops that line, so the report's own last line under
"Cost and speed" says what building it cost: how many questions were embedded, the tokens and
the dollars. Over 90 days of the development database, the invented conversations and some
older test questions, that was 108 questions, 891 tokens and $0.000018.

Scores in the lists are printed to three decimals and the thresholds to two. A score of 0.348
is under a threshold of 0.35; printed as 0.35 it would sit in a list headed "under 0.35"
looking as if it did not belong.

## The signals, and what each one cannot tell

| In the report | From | What it cannot tell you |
|---|---|---|
| Said the information was not there | `fallback_used` | Whether a decline in other words happened. See "What one seeding showed": the flag catches the prompt's wording and a few rewordings, and misses many |
| Answered with nothing close | `top_score` under `LOW_SCORE_THRESHOLD`, and no fallback | Whether the answer was wrong. A short keyword query scores low and can be answered well |
| Asked again in other words | two questions in a row, within ten minutes, at least `REPHRASE_SIMILARITY` alike | Why. It is a hint that the first answer did not land |
| Thumbs down | `message_feedback` | Anything about the answers nobody rated, which is nearly all of them |
| Refused as off-topic | `classification`, with the nearest passage's score | Whether it was a misroute. A high score is a reason to look |
| Articles | the best five passages of each answer's first search | Which passages the model used. It is shown twenty and says nothing of which it drew on |
| Cost and speed | `cost_usd`, `first_token_ms`, `latency_ms` | |

Two of these deserve a paragraph.

**"Answered with nothing close" leaves the fallbacks out on purpose.** A fallback with a low
score is the assistant working as intended, and it is already counted one line up. The score
earns its place on the other case: no passage came close, and an answer was written anyway.
That is where an invented fact would be, if there were one.

**Articles are counted on the best five, not on everything retrieved.** A search returns twenty
passages of about 120. Over a few dozen answers nearly every section is retrieved by something,
so "never retrieved" finds nothing. Five is small enough to mean "this is probably what the
answer was written from".

**They are counted by `doc_id` and section slug, never by chunk id.** A chunk id lasts until
its article is indexed again, and an article is indexed again every time it is edited: its
chunks are deleted and inserted with new ids. The first version of this count joined on chunk
ids. An edited article then lost every earlier answer that had drawn on it, and looked barely
used the day after it was rewritten, while the report's own note said nothing had been lost
(the review of this stage proved it by hiding one article's ids). So each search now records,
beside its chunk ids, the `doc_id` and section slug of every passage (`agent.Search`), and the
count reads those. It is the same lesson as `top_score`: record at answer time what cannot be
worked out later. The change was made on 2026-10-13 and takes effect in production with the
deploy that carries it. An answer stored by the code before it has chunk ids only. It is left
out of the counts, and the report says how many were.

Past 50 counted answers the report also names the sections that were never among any answer's
best five. On fewer, an unused section says more about how little was asked than about the
section.

## Invented conversations

The step was planned for after the site had traffic. It had none, and none was expected for
months, so it was built against conversations nobody had.

`datasets/traffic/invented.yaml` holds 28 of them, 97 questions, written by hand: recruiters,
people with a project, developers curious about the assistant, passers-by. Each message is
labelled with what it was written to be:

- `answerable`: the knowledge base covers it (58)
- `gap`: it does not, with a `topic` naming which of six gaps it belongs to (26)
- `small_talk` (7) and `off_topic` (6)
- `rephrases_previous`: the message before it, asked again in other words (8)

`analytics seed` plays each conversation through `assistant/conversation.chat`, the function a
visitor's message goes through. So the routes, scores, fallbacks and costs in the report are
real. Only the questions are made up. It then moves each conversation back by its `days_ago`,
so that a window has something to select.

**It refuses anything but `ENVIRONMENT=local`.** A stored turn does not say where it came from.
In production an invented conversation would be counted with the visitors', and nothing could
separate them afterwards. Locally they can be told apart by a session id that starts with
`invented-`, which no browser mints, and `seed --clear` removes them by it.

The labels are intentions. The assistant may route or answer a message differently, and that
difference is the useful part.

### What one seeding showed (2026-10-13)

97 questions cost $0.07 to answer.

**Routing.** All 58 answerable and all 26 gap questions were routed to the knowledge base, and
all 6 off-topic ones were refused. Of the 7 written as small talk, 2 were routed to the
knowledge base ("what can you do?" and "ok. are you a real person?"), which answers them
properly too.

**The fallback flag catches fewer than half of the declines on new questions.** No answerable
question was flagged. Of the 26 gap questions, 11 were. The other 15 answers all declined,
wholly or in part, in words the phrase match does not know:

- "I don't have reliable information about Mihail's personal hobbies"
- "Go isn't listed among Mihail's documented technologies or projects, so I can't confirm"
- "The information available doesn't confirm whether he currently has the right to work there"
- "I found no mention of a personal newsletter or YouTube channel"

On the eval answers the same match found 41 of 43 declines (docs/EVALS.md, "gpt-6-luna").
Those were answers to the same handful of unanswerable questions, asked again in every run.
Twenty-six new ones brought out wordings it had never seen.

A phrase match also cannot tell "the knowledge base does not say" from "the knowledge base says
no". One of the stored answers ends "it doesn't have a dedicated mobile app", about Glotsmith.
That is a fact the knowledge base states, in the grammar of a decline. So the flag is a floor
under the true count, and the report's "Said the information was not there" should be read
that way.

**A wider match was measured the same day, and not adopted.** The candidate let the prescribed
sentence take any one word before "information", as in "I don't have reliable information
about". On the invented questions it flags four more of the 26, three of them plain declines.
On the 411 eval answers a judge has read by now, it finds none of the 48 declines that the
match does not find already, and flags three more of the 363 the judge read as answers. All
three are answers to "Where did Mihail do his PhD?" that say there is no information about a
PhD and then give his degrees. A reply the match flags is sent without its sources
(docs/ASSISTANT.md), so the change would take a misleading "Sources:" line off three declines
and a useful one off four answers, and add one question to the report that the score does not
already list. No pattern separates "I don't have information about whether he has a family"
from "I don't have information about where he completed a PhD". If the flag proves too leaky on
real traffic, the fix is a model reading each answer for the report, with the pattern left
narrow for the one thing a visitor sees.

**The score separates the two kinds of question only roughly.** How many scored under each
threshold, with the eval baseline's 50 knowledge-base cases (`v3-baseline`) beside them:

| Under | Invented gap (26) | Invented answerable (58) | Baseline, expects a fallback (5) | Baseline, the rest (45) |
|---|---|---|---|---|
| 0.30 | 7 | 1 | 4 | 0 |
| 0.32 | 11 | 1 | 5 | 2 |
| 0.35 | 18 | 3 | 5 | 2 |
| 0.38 | 21 | 7 | 5 | 3 |

`LOW_SCORE_THRESHOLD` is 0.35: 23 of the 31 questions the knowledge base cannot answer score
under it, and 5 of the 103 it can. It errs towards listing too much, because what it draws is a
list a person reads. At 0.35 the "Answered with nothing close" list holds 9 of the 15 declines
the flag missed, and 3 answers that were fine. The flag and the score together reach 20 of the
26 gap questions.

**Asked again.** The similarity of each labelled pair, and of the 56 other pairs of questions
asked one after the other:

| At or over | Labelled as asked twice (8) | Other pairs (56) |
|---|---|---|
| 0.45 | 7 | 7 |
| 0.50 | 5 | 4 |
| 0.55 | 4 | 1 |
| 0.60 | 4 | 0 |
| 0.70 | 1 | 0 |

`REPHRASE_SIMILARITY` is 0.60, the lowest value that called no other pair a rephrase. It finds
half of the real ones: "What's his day rate?" and "How much does he charge per day?" score 0.74,
while "How senior is he?" and "How many years of experience does Mihail have?" score 0.34, lower
than two different questions about his stack. It errs the opposite way to the score threshold
on purpose. A pair wrongly called a rephrase says an answer failed when it had not.

**Both thresholds are provisional.** They rest on one seeding of invented questions and on
eval cases. Each report ends with the two distributions they should be set again from, once the
questions are visitors'.

## The commands

All run from the repository root.

```bash
uv run python -m portfolio_ai.analytics report               # the last 7 days
uv run python -m portfolio_ai.analytics report --since 30d

uv run python -m portfolio_ai.analytics seed --dry-run       # the plan; spends nothing
uv run python -m portfolio_ai.analytics seed                 # about $0.10; local only
uv run python -m portfolio_ai.analytics seed --clear         # take them out again

uv run python -m portfolio_ai.analytics purge --dry-run      # the retention sweep (docs/API.md)
```

**`report` quotes visitors, so it is for a terminal and never for the crontab.** Under
supercronic a command's output is the container's log, and nothing a visitor types goes into a
log. On the server it is run by hand, where the output goes to the terminal that asked:

```bash
docker compose exec worker python -m portfolio_ai.analytics report --since 30d
```

`report` embeds the window's questions and writes nothing. It logs at warning level only, so
that the report is not preceded by lines about the pool opening, and prints what the embedding
cost as its own last line of "Cost and speed".

`seed` refuses when invented conversations are already stored: playing them twice would double
every count, and a conversation's second playing would be answered with its first as history.
If an OpenAI call fails part-way, `seed --clear` and seed again. `--clear` and `--dry-run`
together count what would be removed. The environment is checked before the file is read: the
image has no `datasets/` folder, and on the server the answer has to be that this does not run
there, not that a file is missing.

## Which piece does what

| Module | Role |
|---|---|
| `db/analytics.py` | every query the analytics make: the window's turns, the volume before it, article usage, and the three statements `seed` needs |
| `assistant/agent.py` | `Search`, the record of one search stored with each answer, which now carries the `doc_id` and section of every passage |
| `strict_yaml.py` | YAML in which a key written twice is an error; the invented file and the eval datasets are both read through it |
| `analytics/signals.py` | arithmetic with no database and no model: cosine similarity, consecutive pairs, median and 90th percentile, counting by band |
| `analytics/report.py` | `window`, `build` (the queries and the embedding of the questions) and `render` (the two forms) |
| `analytics/seed.py` | the invented file's format and rules, playing it through the assistant, clearing it, and the local-only guard |
| `analytics/cli.py` | `report`, `seed`, and the older `purge` |
| `analytics/retention.py` | the nightly sweep (docs/API.md) |
| `datasets/traffic/invented.yaml` | the invented conversations. In a folder of its own, because the unit tests load every `datasets/*.yaml` as an eval dataset |

## Python worth knowing here

**`math.sumprod`.** New in Python 3.12, the project's floor. `math.sumprod(a, b)` multiplies two
sequences element by element and adds the results, in C. On two lists of 1,536 floats it took 26
microseconds here, against 109 for `sum(x * y for x, y in zip(a, b))`. A cosine is three of
them. A few hundred comparisons take a few hundredths of a second, which is why there is no
numpy in `pyproject.toml`. Retrieval does the same arithmetic over the whole knowledge base, and
that happens in Postgres (`<=>`), where the index is.

**`itertools.groupby` only groups neighbours.** It walks a sequence once and starts a new group
every time the key changes. Handed turns that are not sorted by conversation, it yields the same
conversation several times. `consecutive_pairs` sorts first, and a test hands it turns out of
order. This is the opposite of SQL's `group by`, and of what the name suggests to anyone coming
from there.

**`itertools.pairwise`.** `pairwise([a, b, c])` yields `(a, b)` and `(b, c)`: each item with the
one before it, which is the asked-again check exactly.

**`statistics.quantiles(values, n=10, method="inclusive")`.** Nine cut points between ten equal
groups; the last is the 90th percentile. The default method treats the values as a sample of
something larger and extrapolates: for 1, 2 and 3 it reports 3.6, a 90th percentile slower than
the slowest answer. `inclusive` treats them as everything there is and reports 2.8.

**`sum(..., start=Decimal(0))`.** `sum` starts from the integer 0 unless told otherwise, so
summing no Decimals at all returns an int. Nothing fails at once: `0` formats as money as well
as `Decimal(0)` does. But the property says it returns a `Decimal`, and it would return one on
every day but the empty one. mypy says so before any test does: without `start`, the type of
the sum is `Decimal | Literal[0]`, and returning that from a function annotated `Decimal` is an
error.

**A float on a band's edge.** `0.3 / 0.05` is `5.999999999999999`. Floored as it stands, a score
of 0.30 is counted in the band below its own. `histogram` rounds to nine places before flooring.

**An async generator has to be run to its end.** `conversation.chat` yields events while an
answer is written and stores the turn when it reaches the last one. `seed` wants only that last
event, and still loops over all of them: stopping early would leave the turn unstored.

**Properties on a frozen dataclass.** `Report` stores what was read and computed, and its lists
(`fallbacks`, `thin`, `asked_again`) are `@property` methods that select from it. Nothing has to
be kept in step, and a test can build a `Report` with other thresholds and read different lists
from the same data.

**SQL worth knowing here.**
- `jsonb_array_elements_text(...)` turns a JSON list into rows. `with ordinality` numbers them
  as they come out, which is how "the best five passages" is asked of a list stored best first
  on the answer's row.
- `rows from (f(a), g(b))` runs two such functions side by side, so the first `doc_id` sits
  beside the first section slug. Two separate joins would pair every one with every other.
- A function in a `from` clause may read the columns of the tables before it. A subquery in the
  same place may not, unless it is written `lateral`. The word is allowed before a function
  too, where it changes nothing.
- `is not distinct from` is `=` that treats two nulls as equal. Plain `=` between two nulls is
  null, which a `where` reads as no.
- `starts_with(session_id, 'invented-')` rather than `like`, because in a `like` pattern an
  underscore means "any one character".

## How it is tested

- **`tests/unit/test_analytics_signals.py`**: the arithmetic on vectors of two dimensions, where
  the right answer can be read off a sketch. Pairs are never made across conversations, across
  small talk, or more than ten minutes apart, and turns handed over out of order are still
  paired correctly. The 90th percentile never exceeds the slowest value. A value on a band's
  edge is counted in its own band.
- **`tests/unit/test_analytics_report.py`**: the window's parsing and its refusals; what each of
  the report's lists selects; and the two forms of the text. **The emailed form is rendered and
  searched for every question, answer and comment in the report**, and for the passage a
  refused question was near. That test is the privacy rule, written so that it fails: the
  review made `render` quote in memory and watched it go red. Also: a score just under the
  threshold is not printed as the threshold, unused sections are named only past 50 counted
  answers and never in the emailed form, and both forms say what building the report cost.
- **`tests/unit/test_analytics_seed.py`**: the committed file loads and is the size its header
  says; it holds no address and no link; every rule of the format is broken once and refused,
  a field written twice included. Seeding, clearing and the dry run's count each refuse
  production, with the database replaced by something that fails the test if it is touched.
- **`tests/unit/test_analytics_commands.py`**: `report` and `seed` as commands. Which function a
  flag reaches, what is printed, and the exit code. A refusal is one line and exit code 1, and
  it comes before a missing file is noticed. A console that cannot encode a character prints
  `?` instead of stopping.
- **`tests/unit/test_agent.py`**: a search records the `doc_id` and section of each passage, in
  the order of its chunk ids.
- **`tests/unit/test_analytics_cli.py`**: `purge`, as before.
- **`tests/integration/test_analytics_db.py`**, against Postgres: the window is on the
  question's time and open at its end; an unrated answer is still a turn; an article is counted
  for an answer only when it is among the best passages of the first search; **an article keeps
  its answers when it is indexed again**, which is the regression test for the chunk-id count;
  an answer stored before articles were recorded is counted as searched and credited to
  nothing; a section with no slug is still matched; a prefix matches what starts with it and
  an empty one is refused; backdating moves one conversation, its messages and its thumbs, and
  keeps its shape.
- **`tests/integration/test_analytics_seed.py`**: two conversations played end to end with only
  OpenAI faked. They are stored under `invented-` ids, the second question is answered with the
  first as history, the thumbs down lands on its answer, and each conversation is as old as it
  was asked to be. Seeding twice asks nothing. Clearing leaves a visitor's conversation alone.
- **`tests/integration/test_analytics_report.py`**: a report built from stored turns, with the
  fake deciding how alike two questions are. One request for five questions; a question two
  visitors asked embedded once; small talk not at all; and with `EMBEDDING_BATCH_SIZE=2`, three
  requests. The refused question nearest a passage comes first. **The two thresholds are set
  through the environment and the test runs twice**, with two pairs of values and two expected
  sets of lists, so a `build` that ignored either setting, or swapped them, fails one run.

## Not built yet

Decided, and to be built in this order:

- **The weekly email** (`digest`). The counts and the content gaps' names, and nothing a visitor
  typed. Sent through Gmail SMTP with `smtplib`. It sends nothing in a week when nobody asked
  anything. A failed send loses nothing: the questions are in the database for 90 days and the
  command is run again.
- **Content gaps** (`refresh-gaps`, `gaps`). The questions that failed are clustered and each
  cluster is named by a model. `content_gaps` will hold the ids of example questions and not
  their text, because the table is kept indefinitely and a visitor's words are not. A question
  counts as failed when its answer was a fallback or scored under `LOW_SCORE_THRESHOLD`. In the
  seeding above that is 20 of the 26 gap questions, and at least two from every one of the six
  topics, with 3 of the 58 answerable ones beside them.
- **`promote-case`**. Prints a question as a ready YAML case. Promoted cases wait in
  `datasets/promoted.yaml`, which is checked and never run, and join the next golden dataset
  when the knowledge base has changed enough to need one. A change of model still needs no
  change of dataset.
