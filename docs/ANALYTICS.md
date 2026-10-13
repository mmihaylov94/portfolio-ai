# The analytics, end to end

How the project reads its own chat history back, module by module:
- what an answer records about itself, and what a report makes of it
- why one form of the report quotes visitors and the other never does
- how it was built and checked before the site had visitors

The code is in `src/portfolio_ai/analytics/`, the SQL in `db/analytics.py`, and the invented
conversations it was built against in `datasets/traffic/`. ARCHITECTURE.md §10 has the design.

**Built so far:** the report (`analytics report`), the weekly email of its counts
(`analytics digest`), the content gaps (`analytics refresh-gaps`, `gaps`, `gap-status`) and the
invented conversations (`analytics seed`). The nightly retention sweep, `analytics purge`, is
older and is described in docs/API.md, "Retention". **Not built yet:** `promote-case`. The last
section says what is decided about it.

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
- **What is derived is thrown away, with one exception kept on purpose.** The one thing the
  report computes that was not recorded is how alike two questions are, which needs their
  embeddings. They are made when the report is built and dropped when it ends. A table of them
  would be one more thing made from visitors' words to keep in step with the purge, to save a
  fraction of a cent. The exception is a content gap, which keeps a name and a centre for good.
  "What the table keeps, and what it does not" says what those are and why.

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
8. **The content gaps** (`db/analytics.list_gaps`), read from the table as the last refresh
   left them. Building a report does not work them out again.
9. **The text** (`report.render`). `verbatim=True` for the terminal.

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

## The weekly email

`python -m portfolio_ai.analytics digest` is run by the crontab, as `purge` is: on Mondays at
05:30, for the seven days before. It is the report's second form, the counts without the words,
with the content gaps by name, sent to one person.

What it does, in order:

1. **`--since` is read**, before anything else. A mistyped one in the crontab line then fails
   on its first Monday, and not only from the day the mail settings arrive.
2. **Are the mail settings there?** (`mail.missing_settings`). Three have no default:
   `SMTP_USER`, `SMTP_APP_PASSWORD` and `DIGEST_TO_EMAIL`. With any missing it logs
   `digest_not_configured` as a warning, naming them, and stops with exit code 0. Nothing is
   read or spent. It is the same choice the purge makes with retention switched off: the email
   is optional, and a job that failed every Monday for want of it would teach whoever reads the
   logs to skip past failures.
3. **The content gaps are worked out again** (`gaps.refresh`), before the report reads them.
   In a week nobody asked anything too: the purge goes on taking questions every night, and a
   gap's count has to follow them down. A dry run skips this: a refresh writes to the table and
   pays a model for names, and a dry run shows the gaps as they stood.
4. **The report** (`report.build`), exactly as `report` builds it. Its gaps are the table as
   the refresh has just left it.
5. **Did anybody ask anything?** If not it logs `digest_skipped` and stops. Months without a
   visitor are months without an email.
6. **The message** (`digest.compose`). The subject is the week in a line, for an inbox that
   shows only subjects: "mihaylov.io chat: 36 questions in the 7 days to 2026-10-13, 3 not
   answered". The body is `render(report, verbatim=False)` and a last paragraph. It says that
   the email holds none of what visitors typed and that the gaps' names were written by a
   model from their questions, and it gives the two commands that print the questions:
   `report`, and `gaps`. The `report` command looks back a day further than the email does
   (`--since 8d` for a week): `--since` counts from the moment the command is run, which is
   hours after the email was sent, and the same number of days would begin after the email's
   first questions.
7. **The send** (`mail.send`), after the event loop has finished. See below for why.

**The gaps' counts are not the week's.** Everything else in the email is counted over its
seven days. A gap counts every failed question still stored, which is up to 90 days of them,
and the section says so in its first line. It lists the ten most asked and counts the rest.

It logs `digest_sent` with the counts and never with the address it went to.

| Outcome | Exit code | Log line |
|---|---|---|
| Sent | 0 | `digest_sent`, with counts |
| Mail settings missing | 0 | `digest_not_configured` (warning), naming them |
| Nobody asked anything | 0 | `gaps_refreshed`, then `digest_skipped` |
| The send failed, or the login was refused | 1 | `command_failed`, with the kind of failure |
| The report could not be built, the gaps could not be refreshed, or `--since` is not a number of days | 1 | `command_failed` |

**A failed send loses nothing.** The questions are in the database for 90 days, so the command
is run again, with `--since` set to reach back as far as is wanted. Nothing is written to disk.

Two flags are for a person:

- `--dry-run` prints the subject and the body and sends nothing. It needs no mail settings,
  which makes it the way to read the email before the mailbox exists.
- `--test` sends a one-line message and builds no report. It proves the three settings and
  nothing else. Without them it fails and names what is missing: somebody asked for a test,
  and "not set up" is the answer.

**The conversation with the mail server** (`mail.send`), in `smtplib`'s words:

1. `smtplib.SMTP(host, 587)` connects in plain text, which is how port 587 starts.
2. `starttls(context=ssl.create_default_context())` upgrades the connection to TLS, and the
   context is what makes that mean something: it checks the server's certificate against the
   system's trusted authorities, and that it was issued for this host. Left out, `starttls`
   falls back to a context with both checks off (so it is on Python 3.12). The connection is
   then encrypted to whoever answered.
3. `login`, after step 2 and never before: the password goes over the encrypted connection.
4. `send_message` hands the message over.

The message carries a `Date` and a `Message-ID` of its own. Gmail's server adds both when they
are missing; another `SMTP_HOST` may not. The id is made on the sender's domain, because left
to itself `make_msgid` uses the machine's host name, and a container's name has no business in
a mail header.

A rejected login is a `ConfigError` that gives the server's code, names the two settings and
says Gmail wants an app password. The code matters: 535 is a refused name or password, and a
454 is the server asking to be tried later. Everything else that can go wrong in that
conversation is a `MailError`: a refusal, a server that cannot be reached, a timeout, a
certificate that does not verify, and a name or password `smtplib` cannot encode, which it
raises as `UnicodeEncodeError` before it has contacted anyone. A `MailError` gives the kind of
failure and the SMTP code, and never repeats what the server said: a refusal quotes the
addresses it is about, and the message is logged.

**Why the send is not `async`.** `smtplib` blocks: each call waits for the server before it
returns. Inside the event loop that would stop every other task for as long as it took, which
is the lesson-5 trap. The command does its database and OpenAI work inside the loop, lets the
loop finish, and then sends from ordinary code with nothing left to hold up.
`asyncio.to_thread(send, ...)` would be the tool inside a server, where the loop has other work
to do; one command does not need the machinery.

## Content gaps

A content gap is a group of questions the knowledge base could not answer that one article
would close. `refresh-gaps` works the groups out from everything still stored and brings the
`content_gaps` table up to date. The report and the weekly email read the table, and `gaps`
prints it as a writing queue.

### The idea: a group is its centre

Each search is an arrow in the embedding space. A group is summed up by one arrow pointing
where its members point on average, its centre, and a search belongs to the group whose centre
it points most nearly along, if that is near enough. "Near enough" is `GAP_SIMILARITY`.
`clustering.cluster` does it in rounds of two passes:

1. **Make the groups.** Take the searches in order. Each joins the nearest group when its
   centre is at least `GAP_SIMILARITY` alike, and the centre is moved to take it in. Otherwise
   it starts a group of its own.
2. **Settle them.** The centres moved as members arrived, so an early search may now sit nearer
   a group that did not exist when it was placed. Every search is placed again.

A search the second pass finds near no centre at all, because its own group drifted away from
it, is a stray. **The strays get a round of their own**, and theirs another, until none are
left. The first version set each stray aside as a group of one. The review of this stage showed
what that does: two visitors who ran the very same search, both left behind by one drifting
group, became two gaps, because neither was ever compared with the other.

No library does this here. scikit-learn would be the largest dependency in the project, brought
in to group two dozen sentences, and its k-means needs the number of groups in advance, which
is the one thing not known. The price of doing it by hand is that the result depends on the
order; the searches are always handed over in id order, so the same ones give the same groups.

### One refresh, start to finish

1. **The questions that failed** (`db/analytics.failed_questions`): the stored knowledge-base
   answers that were a fallback, or whose best passage scored under `LOW_SCORE_THRESHOLD`. On
   the invented conversations that is 20 of the 26 written to be unanswerable and 3 of the 58
   written to be answerable. At most the newest 500 are read ("The limits of one refresh").
2. **Their searches are embedded** (`gaps._embed`), each distinct one once.
3. **They are clustered** (`clustering.cluster`).
4. **Each group is matched to a stored gap** (`db/analytics.nearest_gap`), by centre: the
   same `<=>` retrieval uses, on a different table. A group whose centre is at least 0.75 like a
   stored gap's (`gaps.SAME_GAP`) is that gap. Two groups of one refresh cannot both claim one
   gap.
5. **The ones with no name are named** (`gaps._name`) by the classifier's model, one call
   each, from `prompts/gap_label.md`: the new gaps, and any stored gap still called "Unnamed
   gap". At most 20 in one refresh.
6. **Everything is written in one transaction** (`db/analytics.apply_gap_refresh`). A known
   gap gets this refresh's counts and keeps its label and status. A new one is inserted. Every
   other gap that still had questions is emptied.

**The order protects the table.** Everything that can fail on its own, the embeddings and the
names, is done before anything is written. A refresh that dies at OpenAI on its third name has
changed nothing, not even the first two.

`refresh-gaps --dry-run` does steps 1 to 4 and stops. It says how many gaps the refresh would
make, which stored gap each group is, and how many stored gaps it would empty.

**Why 0.75 makes two groups one gap, and what it gets wrong.** Measured on the 24 failed
questions in the development database:

| | How alike the two centres were |
|---|---|
| Two different gaps | at most 0.47 |
| One gap, before and after it gained or lost a question | at least 0.86 |
| One gap, first seen as a single question and then as its whole group | at least 0.86 |
| One gap, with every question replaced by others of the same group | as little as 0.47 |

The last row is each group split into two parts that share no question, and the least alike
split of each: from 0.47 for the loosest group to 0.84 for the tightest. A gap that changes a
question at a time is recognised with room to spare. One whose questions were all replaced
cannot be told from a different gap by any value, and here it becomes a new gap while the old
one is emptied.

The two mistakes do not cost the same, which is why the line sits nearer the top of the space
between. Calling two subjects one gap files the new subject under an old label and an old
decision, and nobody is told. Calling one gap two puts a second row in the queue, where a
person sees it. The arithmetic edge of the second kind: a gap of one question that gains four,
each exactly 0.45 like it and like each other, moves to 0.748, is taken for new, and loses its
`wont_fix`.

### A label is a model's output, and is treated as input

A label is written by a model from what visitors typed, kept for good, and printed in a
terminal and in an email. That makes it input, and `gaps._fault` checks it like input. A label
is refused when it is:

- **empty, or over 60 characters**: a refusal, or a paragraph where a title was asked for;
- **not plain text** (`str.isprintable()`): it holds a control character or an invisible one.
  Postgres refuses a NUL in a `text` column outright, which would have ended a Monday's digest
  in a traceback and no email. An escape sequence clears or recolours the terminal it is
  printed in. The API refuses control characters from visitors, and a model's output is owed
  no more trust than theirs;
- **a link or an address** (it holds `://`, `www.` or `@`): what somebody who had talked the
  model into something would want it to write.

A refused label becomes "Unnamed gap", and the log line gives the reason and the length, never
the label. The next refresh asks again, and so does every one after it until an answer can be
used.

What the model is shown is narrowed too (`gaps.brief`). Each question is flattened to one line
and clipped to 200 characters, and its double quotes are made single, so that a question cannot
end its own line and write what reads as the next one, or as an instruction. The prompt says
that the questions are text to label and not instructions.

**What is not checked.** "Six words or fewer" and "never name who asked" are requests, with
nothing behind them: one label of the first run had seven words. Whether an instruction typed
into a question can steer the model was not tried, since that takes a crafted question and a
paid call; the checks are written for the case that it can. A label naming a person would pass
all of them. That is why `refresh-gaps` prints every label it writes, and why `gaps` shows the
questions behind each.

### The limits of one refresh

Nothing bounded a refresh at first, and the daily spending limit does not cover it.

- **500 failed questions** (`gaps.MAX_QUESTIONS`), the newest. Grouping compares every search
  with every centre, so its time grows with the square of the number. 500 searches with nothing
  in common took 11 seconds on the development machine; at that rate 5,000 would take twenty
  minutes, on a server that is also serving the site. The spending limit allows about 1,600
  answers a day, so the number stored is not small by nature. Past the limit `refresh-gaps`
  says that older ones were left out, and a gap whose questions are all among those is emptied
  as though they had been purged.
- **20 names** (`gaps.MAX_NAMED`), the most asked gaps first. The rest are stored as "Unnamed
  gap" and named by the refreshes after it. Twenty calls are about a third of a cent.
- **10 gaps in a report** (`report.GAPS_LISTED`), the most asked, with a line counting the
  rest. `gaps` lists them all.

**Two refreshes at the same moment are not guarded against.** Each compares its groups with
the table before either has written, so a gap new to both is inserted twice. The next refresh
recognises one of the two and empties the other, which stays as a row with no questions: last
in `gaps`, and in neither the report nor the email. It takes the Monday run and a refresh by
hand within the same few seconds.

### It is the search that is clustered, not the question

This was found by doing it the other way first. Clustered as visitors typed them, the invented
questions grouped by their phrasing and by one word:

- one group of eight held a blog, conference talks, client references, hobbies, a family and
  Salesforce: short questions of one shape, about nothing in common;
- another held the six questions that say "Mihail", among them "Who is Mihail anyway?", "Where
  is Mihail based then?" and "How much does Mihail charge?", and none that say "he".

Every knowledge-base answer has also stored the query the model searched with (`tool_calls`):
a few topical keywords, with the name and the filler taken out, and with a follow-up's context
put back, so that "What about Go?" was searched as "Go programming language experience".
Clustered by those, the same 23 questions fell into 13 groups that a person would recognise:
consulting rates (4), salary (3), references (3), work authorisation (2), hobbies (2), and so
on. Six values of `GAP_SIMILARITY` were tried. 0.40, 0.45 and 0.50 gave the same 13 groups. At
0.35 there were 6, with unrelated subjects merged; at 0.55 and at 0.60 there were 14, one pair
having split. The default is 0.45, the middle of the three that agreed, and it is provisional
like the other two.

The rewrite was built to make retrieval work (docs/ASSISTANT.md). It is also what makes this
work, and it was already being stored.

### What the table keeps, and what it does not

`content_gaps` is kept indefinitely; chat is deleted after 90 days. So a gap holds:

- **a label**, written by a model. It is told to name the missing topic in six words or fewer
  and never to name or describe who asked. It is the one piece of text derived from visitors'
  questions that outlives them, which is why it is checked before it is stored and why
  `refresh-gaps` prints every label it writes;
- **a centre**: 1,536 numbers, the average direction of its searches' embeddings. It is how the
  gap is recognised by the next refresh, and by one a year later, so it cannot be thrown away
  as the report's embeddings are. It is not text, and it is made from the searches the model
  wrote and not from the questions as typed. But for a gap of one question, which six of the
  13 below are, it is the embedding of one search run for one visitor, and it is kept for as
  long as the gap is. That is the cost of recognising a gap that comes back;
- **counts**: how many questions, how many of them were fallbacks, the average best score, the
  share that got a thumbs down;
- **the ids of up to five example questions**, the ones nearest the centre, and not their
  text. An id says nothing by itself and resolves to words only while its message exists.
  Migration 0006 replaced the `example_questions text[]` the table was first given for this
  reason;
- **dates**: first and last asked, last refreshed, and when it was marked;
- **a status**: `open`, `written` (with the `doc_id` of the article) or `wont_fix`.

**A gap nobody asks about any more is emptied, not deleted.** Its questions go with the purge,
the next refresh finds none, and its counts go to zero. The row stays with its label, status
and dates, so that when the subject comes round again a year later it is recognised, and a gap
that was decided against is still decided against.

**A gap marked written that goes unanswered again is called out**, in the report and in
`gaps`. `gap-status` stamps the moment a gap is marked (`resolved_at`), and a question in it
that fails after that moment says the article did not close it. It is flagged and not
reopened: that is a person's call.

### What a refresh showed (2026-10-13)

The development database held 24 failed questions: the 23 invented ones and one older test
question. They made 13 gaps, named for $0.0021. The seven with more than one question:

| Questions | Of them, said "not there" | Label, as the model wrote it |
|---|---|---|
| 4 | 3 | Consulting day rates and service pricing |
| 3 | 3 | Salary and compensation expectations |
| 3 | 1 | Client and employer references and testimonials |
| 2 | 1 | CRM and warehouse systems integration experience |
| 2 | 1 | Birth date and immediate family status |
| 2 | 1 | Personal hobbies and interests |
| 2 | 0 | Work authorization and visa sponsorship status |

The other six have one question each: Terraform, Go, a blog, conference talks, "Short
professional biography and background" and "Current location / base city".

These are the figures of the refresh run after the review of this stage, with the checks above
in place and two lines added to the prompt. The first run, earlier the same day, made the same
13 groups for $0.0019 and worded several of the labels differently.

- **A second refresh recognised all 13 and named nothing**, for the $0.000003 of embedding the
  searches again.
- **Every label kept to six words this time. One of the first run's had seven**, and was
  exactly 60 characters: "CRM and warehouse system integrations (including Salesforce)". The
  number of words is a request; 60 characters is what the code checks.
- **Another of the first run's had no capital**: "client references and testimonials", though
  the prompt asks for sentence case. The code now gives a label one. Only the first letter is
  touched, so "n8n" inside a label is left alone.
- **Two of the 13 are not gaps, and a third is half one.** "Who is Mihail anyway?" (the
  biography gap) and "Where is Mihail based then?" (the location one) were answered well and
  are here because their scores were low. The CRM group holds the Salesforce question beside
  one about a logistics business that was answered well too.
- **Nothing in the counts tells those from real gaps.** "0 said the information was not there"
  is a hint and no more. Five gaps show it, and three of the five are real: work authorisation,
  Terraform and Go were each declined in words the flag does not catch, as 15 of the 26
  declines were ("What one seeding showed"). Marking by that figure would have taken three real
  gaps out of the queue and the email for good. The way to tell is to read the questions,
  which is what `gaps` is for, and then `gap-status ... wont_fix`.

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

uv run python -m portfolio_ai.analytics digest --dry-run     # the email, printed; sends nothing
uv run python -m portfolio_ai.analytics digest --test        # one line, to prove the mail settings
uv run python -m portfolio_ai.analytics digest               # what the crontab runs on Mondays

uv run python -m portfolio_ai.analytics refresh-gaps --dry-run   # group and match; name and write nothing
uv run python -m portfolio_ai.analytics refresh-gaps             # a cent or less
uv run python -m portfolio_ai.analytics gaps                     # the writing queue; quotes visitors
uv run python -m portfolio_ai.analytics gaps --status all
uv run python -m portfolio_ai.analytics gap-status 7 written --doc services
uv run python -m portfolio_ai.analytics gap-status 9 wont_fix

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

**`gaps` quotes visitors too**: under each gap it prints up to three of its example questions
that are still stored. It is held to the same rule as `report`. `refresh-gaps` is not, and
prints labels and counts only, because `digest` runs the same refresh on a Monday. It also says
when more failed questions are stored than it grouped, and how many gaps are still unnamed.

`gap-status` refuses what makes no sense: `written` without `--doc`, a `--doc` that is not an
indexed article, `--doc` with any other status, and a gap that does not exist.

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
| `analytics/digest.py` | the email as a subject and a body, composed from a report; the one-line test message |
| `analytics/mail.py` | the mail settings, and `send`: one plain-text message over SMTP, with the failures turned into this project's errors |
| `analytics/clustering.py` | grouping vectors by their centres: `centre_of` and `cluster`, with no database and no model |
| `analytics/gaps.py` | the refresh: failed questions to groups, groups to stored gaps, names for the ones without, one write. Also what a label may hold, and the limits of one refresh |
| `assistant/prompts/gap_label.md` | what the model is told when it names a gap. Versioned and pinned like every prompt, and kept out of the list an eval run records |
| `migrations/versions/0006_content_gaps.py` | `example_questions` goes; `example_message_ids`, `fallback_count`, `refreshed_at` and `resolved_at` arrive |
| `analytics/cli.py` | `report`, `digest`, `refresh-gaps`, `gaps`, `gap-status`, `seed`, and the older `purge` |
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

**`zip(*rows)` turns rows into columns.** `centre_of` adds vectors up coordinate by
coordinate. `zip(*vectors)` hands back all the first coordinates, then all the second ones: the
`*` spreads the list into separate arguments, and `zip` pairs them off by position.
`math.fsum` then adds each column and rounds the total once, correctly. On Python 3.12, the
project's floor, a plain `sum` of floats compensates for its rounding as well and would give
the same centres; before 3.12 it did not, and `fsum` is the one whose name says what is wanted.

**A dictionary's own lookup as a sort key.** `sorted(members, key=alike.__getitem__)` sorts by
`alike[member]`. `alike.__getitem__` is the method that `alike[...]` calls, passed as a
function. `lambda member: alike[member]` does exactly the same and is as safe here, since
`sorted` calls its key at once; this is the shorter way to write it. `alike.get` is the one to
avoid: it answers `None` for a member that is missing, where this raises.

**`dataclasses.replace`.** A frozen dataclass cannot be changed, so naming a gap makes a copy
of its group with one field different: `dataclasses.replace(group, label=label)`.

**`str.isprintable()`.** One call that answers "is this plain text": False when the string
holds any character Unicode files under "Other" or "Separator", the plain space excepted. That
covers the control characters, NUL and ESC among them, and the invisible ones that reverse or
hide text. Letters of any alphabet are printable. It is what stands between a model's label
and the table.

**A slice never raises.** `failed[-500:]` is the last 500 items of a list, and all of it when
the list is shorter. The refresh asks the database for 501 failed questions, learns from a 501st
that there were more than it groups, and keeps the newest 500 with that slice.

**`StrEnum` for a command's choices.** `gaps --status` and `gap-status` take a `StrEnum`. Typer
turns its members into the allowed values, refuses anything else with a usage error before the
command runs, and lists them in `--help`.

**A `real` is not what you put in it.** `top_score` is stored as a `real`, seven significant
digits: 0.35 goes in and 0.3499999940395355 comes out. Compared as it comes, a score of exactly
the threshold counts as under it. The queries round it to six places on the way out.

**`smtplib` and `email.message.EmailMessage`.** Both standard library. `EmailMessage` is the
message: headers set like dictionary keys, and `set_content(text)` for a body, which picks
UTF-8 and an encoding for the wire by itself. `smtplib.SMTP` is the connection, and a context
manager: `with` closes it on the way out whatever happened inside.

**A function in `src/` must not be called `test_something`.** pytest collects every function
whose name starts with `test_` from a test module's namespace, an imported one included. The
message `digest --test` sends is built by `trial_message` for that reason: imported into a test
file as `test_message`, it would be run as a test of nothing and pass.

**Patching a name where it is looked up.** The tests replace `smtplib.SMTP` on the `smtplib`
module, and `mail.py` finds the replacement because it writes `smtplib.SMTP`, which looks the
name up on that module every time it runs. Had it written `from smtplib import SMTP`, it would
hold its own reference to the real class from the moment it was imported, and patching
`smtplib` afterwards would change nothing it could see.

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
- "The newest 500, oldest first" is two orderings, so it is two queries, one inside the other:
  the inner one reads from the highest id down and stops at the limit, and the outer one puts
  what it found back in the order it was asked.
- `coalesce(a, b)` is the first of its arguments that is not null. `set cluster_label =
  coalesce(%(label)s::text, cluster_label)` writes a new label when it is handed one and
  leaves the column as it was when handed `None`.

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
- **`tests/unit/test_analytics_mail.py`**, with `tests/smtp_fake.py` standing in for the mail
  server: the connection is encrypted before the password is sent; the context passed really
  checks the certificate and the host name; the message carries its sender, recipient and body,
  characters outside ASCII included. A rejected login names the settings and repeats neither
  the address nor the password. A refusal gives its kind and code and no address. A server that
  cannot be reached, a timeout, a wrong certificate and a login `smtplib` cannot encode are
  each a `MailError`, and a programming mistake is not dressed up as one. The message is dated
  and its id is on the sender's domain, not this machine's name. **The stand-in refuses a
  connection made while an event loop is running**, which is what pins "the send happens after
  the loop has finished": moved inside it, every test of the command fails.
- **`tests/unit/test_analytics_digest.py`**: the subject and the body; **the body and the
  subject are searched for every question, answer and comment**; and the command. A week with
  questions is sent once, and what is sent is compared whole with what `compose` makes. Without
  mail settings it warns, names them, builds nothing and exits 0. A quiet week sends nothing
  and still refreshes the gaps. A dry run prints the email, needs no settings and sends nothing
  even when it could. A failed send is exit code 1 and one line. `--test` sends one line and
  builds no report. A wrong `--since` fails first, with or without mail settings. The command
  the email prints looks back a day further than the email, and its last paragraph says which
  lines a model wrote. **Everything a scheduled run prints is searched for the questions, both
  addresses, the password and the gap's name**, which is in the email and must not be in the
  log.
- **`tests/unit/test_config.py`**: the mail server defaults to Gmail's and the rest to nothing;
  a blank line means unset; a value that is plainly not an address is refused without being
  repeated, one with a line break and a second header after it and one with a letter outside
  ASCII included. The error is searched for each line of the value: an error that did repeat
  it would print a line break as a backslash and an "n", and the whole value would not be
  found.
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
- **`tests/unit/test_analytics_clustering.py`**: vectors given as angles on a page, so the
  expected groups can be worked out by hand. Largest first; every vector in exactly one group;
  the threshold met by being exactly as alike as it asks. Two tests place a vector early and
  pull its group's centre away from it, one to show it moves to a later group and one to show
  it ends up alone, and a third runs a single pass by hand to show that without the second
  pass it would have stayed put. **Two identical vectors left behind by one group end up
  together**, which is the review's case; two left behind that are not alike stay apart; and
  vectors with no direction, which no round can settle, do not keep the loop going.
- **`tests/unit/test_analytics_gaps.py`**: what is recorded about a group, and that its
  examples are its five most typical questions, the most typical first; what the model is shown
  and with which model, effort and prompt; that only a dozen questions are sent, each on one
  clipped line that cannot close its own quotes; that a label is tidied and capitalised without
  touching a name inside it. **A label with a NUL, an escape sequence, reversed text, a link or
  an address is refused, and the log line holds the reason and not the label**; 60 characters
  pass and 61 do not; a project's name with a dot in it and letters outside ASCII pass. A
  stored gap is named only while it has no name, and one refresh names only so many. And the
  label prompt is pinned but recorded with no eval run.
- **`tests/unit/test_analytics_commands.py`** also covers `refresh-gaps` (labels and counts,
  and neither the question nor the search; a dry run says "would empty"; the lines about
  questions left out and gaps unnamed appear only when true), `gaps` (the examples still
  stored, in order, at most three of four; one lookup for every gap's examples; the alarm only
  on a gap marked written) and every refusal of `gap-status`.
- **`tests/unit/test_analytics_digest.py`** has a stand-in for the refresh in every test, after
  the first run without one spent two and a half minutes waiting for a database that is not
  there. It checks the order: refresh, then build.
- **`tests/unit/test_analytics_report.py`** also covers the gaps' section: the ten most asked
  and a count of the rest; times printed on UTC's clock whatever clock they arrived on; and a
  gap with no question left is not an alarm.
- **`tests/integration/test_analytics_gaps.py`**, against Postgres with only OpenAI faked: the
  first refresh groups, names and stores; a second names nothing and duplicates nothing; a new
  question on a known subject joins its gap and the gap keeps its `wont_fix`; a gap whose
  questions are gone is emptied, kept, and recognised when asked again; a dry run writes
  nothing and counts what would be emptied; **a refresh that fails on its second name leaves
  the table as it was**; a question that fails after a gap was marked written shows on the
  gap. From the review: **nothing the real refresh logs holds a question, a search or a
  label**, with the logger of every module it reaches replaced by a recorder; `SAME_GAP` a
  hundredth either side of the line, through the refresh; two groups that would both match one
  gap, where the second becomes a new gap; a gap's first and last dates kept after those
  questions are purged; a gap left unnamed is named by the next refresh and keeps its status;
  and the two limits, each lowered for the test.
- **`tests/log_recorder.py`** is that recorder. structlog's own `capture_logs()` is not used:
  a logger is cached at its first use, and one cached before a later `configure_logging` call
  is not captured, so a test of what is not logged would pass on an empty list. Checked by
  running it.
- **`tests/integration/test_migrations.py`**: `content_gaps` has the id column and not the
  text one, and migration 0006 goes down to 0005 and back, leaving the old column with no
  default, as 0002 declared it.
- **`tests/integration/test_analytics_report.py`**: a report built from stored turns, with the
  fake deciding how alike two questions are. One request for five questions; a question two
  visitors asked embedded once; small talk not at all; and with `EMBEDDING_BATCH_SIZE=2`, three
  requests. The refused question nearest a passage comes first. **The two thresholds are set
  through the environment and the test runs twice**, with two pairs of values and two expected
  sets of lists, so a `build` that ignored either setting, or swapped them, fails one run.
  The week's email is composed from the stored turns and searched for every one of them. And a
  report carries the content gaps as the table holds them.

## Not built yet

Decided, and the last thing to build:

- **`promote-case`**. Prints a question as a ready YAML case. Promoted cases wait in
  `datasets/promoted.yaml`, which is checked and never run, and join the next golden dataset
  when the knowledge base has changed enough to need one. A change of model still needs no
  change of dataset.
