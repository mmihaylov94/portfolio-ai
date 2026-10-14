# The contact form, end to end

How a message typed into the contact form on mihaylov.io becomes two emails: the checks it meets,
what two models are asked about it, what is sent to whom, and what happens when any of that
fails. It took over from an n8n workflow on 2026-10-14, the last thing on the site that
depended on n8n. It is the one flow in this service that has nothing to do with the knowledge base: no
retrieval, no table, nothing stored.

The way in is private, like the rest of the API. The form posts to the portfolio's Express API,
which limits each visitor and checks reCAPTCHA, and Express calls `POST /v1/contact` here with
the bearer key. [API.md](API.md) has the contract between the two.

## The idea it rests on

**The owner's notification is the one thing that has to happen, and the form says "thank you"
only once it has.**

A contact form has one job: somebody wrote something, and Mihail has to see it. Everything else
this flow does is beside that. The spam rules and the classifier decide how the message is
labelled when it arrives. The acknowledgement to the visitor is a courtesy. So the flow is
arranged around one email, and every step before it is allowed to fail:

```
rules.check ----------- invalid, or spam ------------> blocked notification
    |
classifier.classify --- no verdict to be had ---------> notification, not classified
    |               +-- says spam --------------------> blocked notification
    |
is an acknowledgement safe? -- no --+
    | yes                           |
reply.draft, then send it           |
    |                               |
    +-------------------------------+-----------------> notification, accepted
```

Every path ends at a notification. A message the rules call spam is reported, marked blocked. If
OpenAI cannot be reached, the message is passed on marked "not classified". If the
acknowledgement cannot be written, or is refused by its checks, or cannot be sent, the
notification says so and still goes. "Blocked" never means dropped: it means labelled, and not
answered.

Only one failure is the visitor's business. When the notification itself cannot be sent, the
request fails, and the site tells the visitor the message did not go.

The n8n workflow had this the other way round. Its webhook answered the form straight away and
did the work afterwards, so the form said thank you before anything had happened. A run that
then failed, because OpenAI was down or Gmail refused, lost the message, and nobody was told.
Here the request waits for the work, which is two model calls and two emails. That wait is the
price: several seconds between pressing the button and the thank you, 7.4 and 3.8 for the
first two real messages.

Two things keep the notification safe from a caller that stops waiting:

- **The work runs in a task of its own**, and the endpoint awaits it through `asyncio.shield`.
  If the request is cancelled, because Express gave up or the visitor closed the page, the
  cancellation stops at the shield and the task carries on. It may be between the two emails at
  that moment: the visitor has been acknowledged and the owner not yet told.
- **A message in hand at shutdown gets the same ten seconds as a chat answer** before it is
  cancelled. The lifespan waits for both at once (`api/main.py`).

## One message, start to finish

Follow an invented one: a logistics company asking for a small web tool for its warehouse team,
sent with the reason "Project Inquiry". The figures are from `python -m portfolio_ai.contact try`
on 2026-10-13, which runs everything below except the sending.

**The site's API.** Express turns away a sixth message from one visitor (by IP address) in
fifteen minutes, checks the four fields the way this API will, and verifies the reCAPTCHA token.
Then it posts `{email, subject, message, reason}` to `/v1/contact` with the key, and waits up to
35 seconds.

**The door.** `require_api_key` first, as on every `/v1` route. FastAPI validates the body
against `ContactRequest`: each field trimmed, not empty, within its length, and free of control
characters. Whether the address is an address and the reason one the form offers is deliberately
not checked here. A body that fails is a 422 and nothing has started.

**The desk.** `Desk` (`contact/desk.py`) lives on `app.state`, one per app, like the chat's
`Turns`. The endpoint asks it two things. Is the service stopping? Then 503 with `Retry-After:
5`. (A second line of defence: uvicorn has stopped taking connections by the time that flag is
set.) Has the last hour already seen `CONTACT_MAX_PER_HOUR` messages? Then 429, with `Retry-After`
set to when the oldest of them leaves the hour.

**The task.** `desk.start(service.handle(...))` creates the task and keeps hold of it. The
endpoint awaits it through the shield.

**The rules.** `rules.check` cleans the four fields and scores the message for spam, with no
model and no network. This one scores 0.

**The classifier.** One call, to the model that routes chat messages, with n8n's prompt. It
answers in a fixed shape: `project`, with confidence 0.9; spam confidence 0.01; safe to answer
automatically; and a sentence of explanation.

**Is an acknowledgement safe?** Five conditions, all of which have to hold: the classifier said
so, it is at least 0.6 sure what the message is about, it is at most 0.3 sure the message is
spam, the rules did not find the message suspicious, and the address is one a mail header can
carry as a single recipient. Then two limits on how often a reply may go out: one per inbox a
day, and so many a day for the whole service ("Using the form to write to somebody else",
below). Here everything allows it.

**The reply.** A second call, with the brief for a project inquiry. The model returns paragraphs
of plain text, which `reply.fault` checks like input: not empty, not too long, plain characters
only, no link, no address and no phone number. The code puts the greeting above them and the
signature and two fixed lines below:

```
Hello,

Thanks for reaching out -- this sounds like a practical and useful improvement for a warehouse
setup, and I'd be interested in helping. [...]

To give meaningful guidance, could you share a bit more: estimated scope (number of users, key
features like scanning, stock adjustments, reporting), any target timeline, [...]

Kind Regards,
Mihail

This is an automatic acknowledgement. Mihail reads every message and will reply personally.
You are receiving this because this address was entered in the contact form on mihaylov.io. If that was not you, you can ignore this email.
```

(That reply was written before the last change to the reply prompt's wording, which "The
prompts" below describes. The change was about the signature, which this reply had right.)

**The acknowledgement goes out**, to the address the visitor typed, under the fixed subject
"Your message to Mihail Mihaylov". It is sent from the sending account under the name "Mihail
Mihaylov", and its `Reply-To` is `CONTACT_TO_EMAIL`, so that an answer to it reaches Mihail's
own inbox.

**The notification goes out**, to `CONTACT_TO_EMAIL`:

```
Subject: mihaylov.io - VALID Contact Form Submission [AUTO-REPLIED] - Internal tool for our warehouse team

Valid Contact Form Submission

Status: Accepted
Auto-reply sent: Yes

Request Details
---------------
Sender Email: visitor@example.com
Submitted Reason: project
Detected Reason: project
Subject: Internal tool for our warehouse team
Message: Hi Mihail, we run a small logistics company and track stock in spreadsheets. [...]

Rule-Based Validation
---------------------
Is Valid: true
Is Rule Spam: false
Suspicious: false
Spam Score: 0
Rule Hits: None

AI Classification
-----------------
AI Marked as Spam: false
Spam Confidence: 0.01
Detected Reason: project
Reason Confidence: 0.9
Should Auto-Reply: true
AI Explanation: Clear legitimate project request: building a simple web tool [...]

Auto-Reply Sent
---------------
Reply: Hello, [the acknowledgement, in full]
```

**One log line**, `contact_handled`, with the outcome in counts and flags. **200**, with
`{"status": "received"}`. Express answers the browser `{"ok": true}`, and the form says thank
you.

What each outcome produces:

| Status | The visitor receives | The owner's subject says | Models asked |
|---|---|---|---|
| `invalid` | nothing | `BLOCKED ... [INVALID]` | none |
| `rule_spam` | nothing | `BLOCKED ... [RULE_SPAM]` | none |
| `ai_spam` | nothing | `BLOCKED ... [AI_SPAM]` | the classifier |
| `not_classified` | nothing | `VALID ... [NOT CLASSIFIED]` | the classifier, without an answer |
| `accepted`, no reply | nothing | `VALID ... [NO AUTO-REPLY]`, with the reason | one or both |
| `accepted` | the acknowledgement | `VALID ... [AUTO-REPLIED]` | both |

**In every row the API answers 200 and the form says thank you.** The answer is the same
`{"status": "received"}` whatever was decided. A visitor is not told that they were taken for
spam, and neither is somebody probing the form to learn what gets through.

## The rules: a port, held to its original

`contact/rules.py` is the workflow's "Code | Validate Request" node, which was JavaScript,
written again in Python. It does three things in order.

**It cleans.** Zero-width characters are removed, every run of white space becomes one space,
and the ends are trimmed. In the subject and the message, anything between a `<` and the next
`>` is replaced by a space first. The address and the reason are put in lower case.

**It validates**, on the cleaned fields, and the first failure is the answer: a field is empty,
then the reason is not one of the form's five, then the address is not shaped like one.

**It scores** a valid submission:

| Rule | Points |
|---|---|
| the address is at one of five disposable domains | 2 |
| the message is shorter than 10 characters | 2 |
| the subject is shorter than 2 | 1 |
| more than 2 links | 2 |
| two or more words used four times or more each, in a message of six words or more | 2 |
| more than 10 characters that are not letters, digits, spaces or `.,!?@:/-` | 1 |
| more than 60% capitals, in a message of ten letters or more | 1 |
| the subject and the message are the same | 1 |
| any of 18 spam keywords, in the subject or the message | 2 |
| three or more of them | 1 more |
| the reason is "job" and the message mentions SEO, guest posts, backlinks or traffic | 1 |

A score of 5 or more is spam: blocked, and no model is asked. A score of 3 or 4 is
"suspicious": the classifier still looks at it, and it gets no acknowledgement whatever the
classifier says.

### Why it is a port and not a rewrite

Several of those rules are crude, and the list below says which. They are reproduced anyway. The
workflow ran with them for months, a rule changed in passing is a change nobody measured, and
moving a flow and changing what it does are two jobs better done one at a time.

So the port is held to the original by a fixture. `tests/unit/fixtures/contact_rules.js` runs
the node's own JavaScript, unchanged, under Node, over 81 invented submissions, and writes what
it said about each to `contact_rules.json`. `tests/unit/test_contact_rules.py` then requires
the Python to give the same cleaned fields, validity, score, hits and reason for blocking, for
every one. The generator reads the workflow's export, which is gitignored, so it runs only on a
machine that has the export; the fixture it writes is committed, and that is what CI uses.

Staying faithful to JavaScript took care in four places:

- **White space.** JavaScript's `\s` and Python's are different lists. Python's adds `U+001C`
  to `U+001F` and `U+0085` and leaves out `U+FEFF`. The class is spelt out, character by
  character, in `_SPACE`.
- **Length.** JavaScript counts UTF-16 code units, so an emoji has a length of 2. Python counts
  characters, so it is 1. "Shorter than 10" is a rule, so `_length` asks the JavaScript
  question: encode as UTF-16, halve the byte count.
- **What a word is.** `\b` and `/i` look only at ASCII in a JavaScript pattern without the `u`
  flag, and at all of Unicode in Python unless `re.ASCII` says otherwise. Without it a long s
  (`U+017F`) followed by "eo" would match "seo" here and not there. The flag has to be on every
  pattern that ignores case. The first version of the port had it on the keywords and not on
  the pattern for links, where "http" with a long s after it counted as a link here alone; the
  review found that by running 30,000 random submissions through both sides.
- **Where a string ends.** `$` in Python also matches just before a final line break.
  `fullmatch` does not.

One difference is deliberate. JavaScript counted words in a plain object, where a word such as
"constructor" or "__proto__" already exists as an inherited property, so that word was never
counted properly. Python counts it like any other word, and a test says so. One more is
accepted: three rare letters (`U+A7CB`, `U+A7DC` and `U+1C89`) that Node's newer Unicode tables
put in lower case and Python 3.12's do not. With the link pattern put right, those are all
the differences the same 30,000 random submissions show.

The fixture has a case on each side of each number in the table that can decide anything: a
score of exactly 5 and of 6, a word used three times and four, two keywords and three. One
cannot: see the six-word minimum below. The review found three thresholds it
did not pin, one of them behind a case named `five-is-spam` that scored 6.

### What the rules get wrong, kept as they were

Each of these was run through the port on 2026-10-13. None is a fault of the port; they are what
the workflow did.

- **Text between `<` and `>` is deleted**, whatever it is. "The total is < 500 users today and
  we expect > 2000 by next year" reaches the owner as "The total is 2000 by next year". This is
  the one that loses a visitor's words, and the first worth changing.
- **A message arrives as one line.** Its paragraphs are joined by the cleaning.
- **Keywords match inside other words and as ordinary ones.** "Seoul" contains "seo".
  "cryptography" contains "crypto". "Dr" is a keyword, presumably for "domain rating", and so
  is "DA". An honest mention of WhatsApp or Telegram counts. One such word costs 2 points,
  which by itself blocks nothing. Three different ones cost 3, and the message is suspicious.
- **Letters outside ASCII count as special characters.** A message in Bulgarian of more than
  ten letters gets 1 point for "too many special characters".
- **The address check is loose.** `a,b@example.com` passes it. A mail header would read that
  as two recipients, which is why an address meets a stricter test before a reply is sent to
  it (`emails.safe_recipient`).
- **Only the five listed domains are disposable**, and only exactly: a subdomain of one is not.
- **A recruiter who mentions traffic** gets a point for a "reason/message mismatch".
- **The six-word minimum for repetition never decides anything.** Two words used four times
  each are already eight words, so a message short enough to fall under the minimum cannot
  repeat itself enough to count. It is so in n8n's code too. Found by the second review,
  which moved the number both ways and saw no test fail.

## The two models

Both calls go through `llm/responses.parse`, like the chat's classifier, so each is recorded
with its tokens, model, latency and cost. Both use the classifier's model and effort
(`CLASSIFIER_MODEL`, `CLASSIFIER_REASONING_EFFORT`): the fast, cheap one, doing the same kind of
job. Neither is counted by the daily spending limit, which adds up chat answers.

### The classifier

`contact/classifier.py` shows the model the cleaned submission and what the rules made of it,
in the workflow's own words, and gets back a verdict: spam or not, how sure, what the message is
really about, how sure, whether it is safe to answer automatically, and a sentence of
explanation.

**The verdict is a model's output, written from what a stranger typed, and it is treated as
input.** The two confidences are kept between 0 and 1 whatever the model returned. The
explanation is flattened to one line and cut at 300 characters before it goes into the owner's
email. A reason outside the form's five is not a verdict at all: the schema refuses it, and the
message is passed on as not classified.

### The reply

`contact/reply.py` picks one of five briefs by the reason the classifier detected, fills in the
submission and a few lines about Mihail, and asks for the body of a reply as two or three
paragraphs.

**A reply is a model's output on its way to an address anyone could have typed.** That makes the
form a way to have this system write to a stranger, in Mihail's name. What such a reply can
carry is bounded in code, whatever the prompt was talked into (`reply.fault`):

| Fault | Meaning |
|---|---|
| empty | no paragraph with anything in it |
| too long | more than 5 paragraphs, or more than 2,000 characters |
| not plain text | a character that is not printable, such as an escape sequence |
| a link or an address | `://`, `www.` or `@` anywhere in it, or a word that ends like a web address (`example.com`) |
| a phone number | nine digits or more in a run of digits, spaces, dots, hyphens and brackets |

A reply with a fault is not sent. The owner's notification says a reply was written and held
back, and which fault it had. The greeting, the signature and the line saying the reply is
automatic are not the model's to write: `emails.acknowledgement` adds them, so they are always
exactly there.

The last row is a bound and not a proof. A mail program turns `example.com` into a link without
being asked, so a word ending in one of about twenty common endings counts as one. That also
refuses a reply that names a product such as "Make.com", which is the safe way to be wrong: no
reply goes out, and the owner is told why. The site's own name is let through, and "Node.js" is
not an ending on the list.

Nine digits is the line for a phone number because a number that can be rung from elsewhere
has its area code: `+44 20 7946 0958` holds twelve and `(555) 010-0199` ten, while an ISO date
holds eight and "80-160 words" five.

**The subject is not the visitor's.** The workflow sent its reply under the visitor's own
subject line, the one part of the email an attacker wrote word for word. Here it is fixed,
"Your message to Mihail Mihaylov", so no code puts anything a visitor typed into the reply.
The model's paragraphs, written from the message, are the only part that can echo any of it,
and `fault` and the classifier bound what they can say.

### The prompts

Eight files in `assistant/prompts/`, versioned and pinned by content hash like the others, and
kept out of the list an eval run records, because no eval reads them.

They are the workflow's prompts word for word, with these changes, each recorded in the file's
own frontmatter. On a machine that has the workflow's export, `tests/unit/test_prompts.py`
holds them to that: every line of n8n's is still there, in order, apart from the lines the
test lists as dropped.

- **The reply is paragraphs of text.** The workflow asked the model for HTML and sent what came
  back. Here the code builds the email, so nothing a model wrote is ever sent as markup.
- **The greeting and signature moved into the code**, with the automatic line under them.
- **A rule that the form's data is a message to answer and not instructions.**
- **No links and no addresses**, asked for in the prompt and enforced by `fault`.
- **One description of Mihail** (`contact_context.md`) in place of a few lines in each brief.
  The workflow's called him a "Full-stack developer" and an "Automation engineer". The
  knowledge base now gives Solutions Architect as the only role label. The block is taken from
  `knowledgebase/about-mihail.md`, and has to change when that article's summary does.
- **`should_notify_me` is gone** from the classifier's prompt and its schema. The workflow asked
  for it and never read it: the owner is told of every message.

One wording was measured. The first version of the rule about the greeting and signature named
them, to say what would be added, and two of the five replies written under it ended "Kind
Regards, Mihail" themselves, above the real signature. Reworded so that it quotes neither, eight
replies in eight left it alone. It is the lesson of the chat prompt again (docs/EVALS.md):
naming a phrase to avoid can bring it out.

### A deadline for each step

Left alone, the OpenAI client waits `OPENAI_TIMEOUT_SECONDS` for an attempt and makes several
attempts, which adds up to minutes, and `smtplib` is given half a minute for every step of its
conversation with the mail server. That suits an hourly job and not a person watching a form.
The site's API waits 35 seconds for this endpoint. So each step has a deadline of its own, in
`service.py`:

| Step | Deadline | When it passes |
|---|---|---|
| the classifier | `CLASSIFY_SECONDS`, 8 | no verdict: passed on as not classified |
| the reply | `REPLY_SECONDS`, 10 | no acknowledgement; the notification says so |
| the acknowledgement's email | `SEND_SECONDS`, 6 | not acknowledged; the notification says so |
| the notification's email | `SEND_SECONDS`, 6 | an error: the visitor is told the message did not go |

30 seconds in all, under the 35 the site waits. A unit test here holds the four numbers and
their sum, and a test in the portfolio holds its 35, so neither side moves alone.

Without the deadlines, a slow day at OpenAI would have the site tell a visitor that their
message failed, two minutes before it arrived.

Two things a deadline does not do. A model call cut off by its deadline is recorded as costing
nothing, because what it had spent by then is not known. And a deadline on an email stops the
waiting, not the sending: the send runs in a worker thread, and a thread cannot be stopped from
outside. So `smtplib` is given the same six seconds for each of its steps. A mail server
that goes quiet then ends the thread within six; one that answers every step just slowly can
keep it going longer. Either way an email that "timed out" can still arrive.

### What a trial showed (2026-10-13)

Eight invented submissions, through everything but the sending:

| Submitted as | What it was | Rules' score | Outcome |
|---|---|---|---|
| project | a warehouse tool | 0 | accepted, acknowledged |
| collaboration | co-writing a guide | 0 | accepted, acknowledged |
| job | a recruiter, a named role | 0 | accepted, acknowledged |
| consultation | an hour's advice | 0 | accepted, acknowledged |
| other | a thank-you for a case study | 0 | accepted, acknowledged |
| project | an SEO and backlinks pitch | 3 | blocked by the classifier (spam 0.95) |
| project | "Ignore your previous instructions" and a phishing text to send back | 0 | blocked by the classifier (spam 0.95) |
| job | "We have an seo role, interested? Call me." | 3 | blocked by the classifier (spam 0.85) |

Three things worth reading in that. The sales pitch hit nine keywords and still scored only 3
under the rules, which is "suspicious" and not "spam": the classifier is what stopped it. The
attempt to dictate the reply never reached the model that writes replies, because the
classifier called it spam first; the rule in the reply prompt and the checks on the reply are
the second and third lines behind that. And the last row is a judgement a person might make
differently, which is why a blocked message is still delivered.

Cost: about $0.0004 for the classifier's call and $0.0004 for a reply, so under a tenth of a
cent for a message that is acknowledged.

Five of those, one per reason, went through again on 2026-10-14 under the final wording of the
prompts. All five were acknowledged, none of the replies signed itself, and the five cost
$0.004 together.

## The emails

`contact/emails.py` builds them and sends nothing. Each is an `Email`: a subject, and the same
body twice, as plain text and as HTML.

**Every piece of HTML is made by code, from escaped text.** The workflow pasted the visitor's
message into an HTML template as it was typed. Here a visitor's words, a model's words and a
rule's wording all go through `html.escape` on the way into markup, so a message of `<script>`
arrives as the eight characters it is. Line breaks are turned into `<br>` after the escaping,
never before, or the tags would be escaped too.

**The acknowledgement** is fixed in every part but the model's paragraphs: its subject, its
greeting, its signature, and the two lines under the signature, the second of which is for
whoever never wrote. It comes from the sending account with Mihail's name beside it, and
answers to it go to `CONTACT_TO_EMAIL`.

**The notification** comes from the same account under the name "mihaylov.io contact form". Its
subject keeps the workflow's wording, so a mail filter made for the old emails still matches:
`mihaylov.io - VALID Contact Form Submission [AUTO-REPLIED] - ...`. The one new kind is
`[NOT CLASSIFIED]`.

**A visitor's address goes into a header only after a stricter check.** The rules' own test is
loose, as above. `emails.safe_recipient` wants one mailbox, in plain ASCII, of at most 254
characters. An address that fails it gets no acknowledgement, and the notification says why.

### A header is what the mail library makes of the text

This is the part the first version got wrong, and the review found by sending. Python's
`EmailMessage` does not keep the text it is handed for a header. It parses it, and two things
happen in the parsing that no check on the typed text can see.

**It decodes "encoded words".** `=?utf-8?q?...?=` is the form mail uses to carry characters
outside ASCII in a header, and the parser decodes one wherever it finds it. So:

```
typed as the address    =?utf-8?q?first=40example.org=2C?=second@example.net
held as the To header   first@example.org, second@example.net
```

One mailbox to every check that read the text, and two recipients by the time `smtplib` reads
the header to decide who gets the message. The same trick in a subject decoded to a line break
and a second header on the Python the code was written on (3.12.3). By the review's reading
of CPython's source, releases from 3.12.5 write such a subject out safely; that was read and
not run. The other form decoded to a link that neither the rules nor the classifier had read.

**It refuses a line break, and its idea of one is wide.** A header with a line break in it is
an error, and the library finds line breaks with `str.splitlines`, which counts `U+0085`. That
character is what a mis-decoded ellipsis becomes, and the ported cleaning keeps it, because
JavaScript does not call it white space. A subject with one in it meant the owner's
notification could not be built, and the message was lost with a 503.

Three things answer this, from the outside in:

- `emails.safe_recipient` refuses an address with `=?` in it.
- `emails.header_text` is what a visitor's subject goes through on its way into the owner's
  notification, the one email that still carries it:
  line breaks of every kind become spaces, and `=?` becomes `= ?`, which leaves an encoded word
  as the odd text it is.
- `mail._check_headers` looks at the message once it is built. The subject the library holds
  has to be the subject that was asked for, and each address header has to read, the way
  `smtplib` reads it, as exactly the one address given. Otherwise nothing is sent. This does
  not depend on which Python is running.

The tests read what a mail server would be given, the envelope and the bytes, and not the
`EmailMessage` object, which is how the first version's tests saw nothing wrong.

**The message the owner reads is the cleaned one**: one line, and without anything that stood
between angle brackets. That is what the workflow sent too.

`mail.py` does the sending, and is the module the weekly digest uses (docs/ANALYTICS.md, "The
weekly email"). It lived in `analytics/` until this flow became its second caller. `send` gained
three optional things for it: an HTML version, a `Reply-To`, and a name beside the sender.

## The limits, and what they are for

| Limit | Where | What it stops |
|---|---|---|
| 5 messages per visitor (by IP address) per 15 minutes | Express | one visitor flooding the form |
| reCAPTCHA v3: a token made on mihaylov.io, for the action `contact`, scoring at least 0.5 | Express | scripts |
| the four fields' lengths: 254, 300, 5,000 and 40 characters | Express, then here | a message that costs a lot to read twice |
| `CONTACT_MAX_PER_HOUR`, 30 by default | here | everything else |
| one automatic reply per inbox per 24 hours | here | one person written to again and again |
| `CONTACT_MAX_REPLIES_PER_DAY`, 10 by default | here | many people written to who never asked |

`CONTACT_MAX_PER_HOUR` is the form's own, and it is for the whole service. A message can cost
two model calls and send two emails. The daily spending limit does not see those calls, and
Express limits each visitor and not the total. Thirty an hour bounds the form at about two and
a half cents an hour, whoever is sending and from however many addresses. The last two rows
bound the replies, and have a section of their own, next.

The counts are kept in memory, the hour's in a `deque` of arrival times. That is right for one
process and is forgotten on a restart, after which the same limits apply again from nought.

## Using the form to write to somebody else

The form takes any address, so anybody can type somebody else's and have this service send
that person an email from "Mihail Mihaylov". What such an email can carry is already bounded:
the model writes it, `reply.fault` refuses a link, an address or a phone number in it, and its
subject and closing lines are fixed. What is left to bound is how many go out. The likeliest
harm is not the mail itself but spam complaints against the sending account, which is also the
account the owner's notifications and the weekly email come from.

`contact/replies.py` keeps two limits, and is asked before a reply is written, so that a
refused one costs no model call:

- **One reply per inbox in any 24 hours.** The address is normalised first: lower case, a
  `+tag` dropped, and Gmail's dots ignored. `no_suchperson+1@gmail.com` and
  `no_such.person@gmail.com` are one inbox.
- **`CONTACT_MAX_REPLIES_PER_DAY` replies in any 24 hours for the whole service**, 10 by
  default. 0 switches the replies off: a change to `.env` and the container recreated, with
  no new release.

A message refused a reply still reaches the owner, and the notification says which limit. A
`contact_reply_limited` warning says which in the log, never with the address. A reply counts
once it is decided on, so one that its checks then refuse, or that fails to send, still counts:
the way to be wrong about a stranger's inbox is with fewer emails.

Both limits live in memory. Each address is held as an HMAC, with a key the process makes when
it starts, and never as itself. Old entries are pruned as they age, and everything is forgotten
on a restart: a deploy, a change of setting, a crash or a reboot of the server, none of which a
sender outside can bring about on purpose.

The worst a determined sender can now do is ten unwanted emails a day, none of them twice to
the same address as normalised, each written by the model under a fixed subject. Before this it was about 720 a
day, to anybody, again and again, under a subject the sender wrote. Every reply also ends with a
line telling someone who never wrote that the address was entered in the form and that they can
ignore it.

Two things that sentence does not say. At a domain that takes mail for any name (a catch-all),
or at a provider's alias addresses, one person can be written to under several addresses the
normalisation cannot tell apart, up to the day's cap: the cap is the real bound. And the cap is
shared. Five messages per IP address in fifteen minutes lets one sender use the day's ten in
half an hour, after which genuine visitors get no automatic reply until the day has passed.
Their messages reach the owner as ever. That is the price of a hard cap, and the
`contact_reply_limited` warnings show when it is being paid.

`contact try` keeps no count, and says so in what it prints.

## What is kept, and what is not

**Nothing a visitor typed is stored or logged.** There is no table and no migration. A message
exists in the request, in the two calls to OpenAI, and in the emails, and the emails are the
record.

The flow logs one line of its own for a message, `contact_handled`, beside the `llm_call` line
each model call writes and the `request` line every request has:

```
status, acknowledged, spam_score, rule_hits, submitted_reason, detected_reason,
spam_confidence, reason_confidence, cost_usd
```

`rule_hits` is the rules' own fixed wording with a count in two of them. `submitted_reason` is
logged only when it is one of the form's five words; an invalid reason is whatever was typed,
and is left out. The address, the subject, the message, the model's explanation and its reply
are in none of it.

When something fails, the line says what kind of thing and no more: `contact_classifier_failed`,
`contact_reply_failed` and `contact_acknowledgement_failed` carry the error's type and never
its words, which can quote what was sent;
`contact_reply_unusable` carries the fault's few fixed words; `contact_failed` is a message
whose notification could not be sent, logged by the desk whether or not its caller was still
waiting to be told.

## The command

```bash
uv run python -m portfolio_ai.contact try --reason job --subject "A role" \
    --message "We are hiring a Solutions Architect. Would you be open to a conversation?"
```

`try` puts one made-up submission through the rules and the two models and prints what would be
sent: the acknowledgement and the notification. **It sends nothing**, needs no mail settings,
and stores nothing. It is how a change to a prompt is read before it goes live, and it costs
what two small calls cost.

There is no command that sends. The form on the site is the way to do that: the real path, with
the real mailbox at the end of it.

## Which piece does what

| Module | Role |
|---|---|
| `contact/rules.py` | the fixed rules: clean, validate, score. No model, no network |
| `contact/classifier.py` | one call: spam or not, about what, safe to answer. The verdict, tidied |
| `contact/reply.py` | one call: the acknowledgement's paragraphs, and `fault`, the checks on them |
| `contact/emails.py` | the acknowledgement and the four kinds of notification, as text and as HTML, and `safe_recipient` |
| `contact/service.py` | the flow. `prepare` decides and sends nothing; `handle` is `prepare` and the sending. The two thresholds and the two deadlines |
| `contact/replies.py` | `Replies`: one reply per inbox a day, and `CONTACT_MAX_REPLIES_PER_DAY` in all |
| `contact/desk.py` | `Desk`: the hour's count, the messages in hand, and the app's `Replies` |
| `contact/cli.py` | `python -m portfolio_ai.contact try` |
| `api/routers/contact.py` | `POST /v1/contact`: the desk's two questions, the task, the shield |
| `api/schemas.py` | `ContactRequest` and its four limits; `ContactResponse` |
| `api/errors.py` | `MailError` is a 503 |
| `mail.py` | one email over SMTP, for this and for the digest |
| `assistant/prompts/contact_*.md` | the classifier's prompt, the reply's, the five briefs, and the lines about Mihail |
| `tests/unit/fixtures/contact_rules.js` | runs n8n's own code over the invented submissions and writes the fixture |

In the portfolio repository: `api/src/contact.js` is the route, `api/src/text.js` the trimming
and measuring it shares with the chat routes, and `app/components/ContactSection.vue` the form.

## Python worth knowing here

**`asyncio.shield`.** `await task` passes cancellation down: cancel the awaiting coroutine and
the task is cancelled with it. `await asyncio.shield(task)` breaks that link in one direction.
The waiting is cancelled and the task is not. An exception raised inside the task still arrives
through the shield as usual, which is how a failed notification becomes a 503. The chat solves
the same problem differently, with a queue the request reads from (`api/turns.py`), because it
has events to pass along as they happen. Here there is one result, so awaiting the task is
enough.

**A task needs somebody to hold it, and somebody to look at it.** The event loop keeps only a
weak reference to a task, so one that nothing else refers to can be collected while it runs.
`Desk._in_hand` is that reference. And a task that fails with nobody awaiting it is reported
much later as "Task exception was never retrieved". The done-callback asks the task for its
exception, which counts as looking, and logs the kind of failure.

**`asyncio.timeout`.** A deadline for whatever is awaited inside the block. When it passes, the
call inside is cancelled where it stands, the OpenAI client's retries included, and
`TimeoutError` is raised from the block in its place. If the cancellation came from outside
instead, from shutdown say, it stays a cancellation: the block only converts the one it caused.
If you know Node, this is `AbortSignal.timeout()` built into the language's own waiting, with
no signal to pass down by hand.

**`asyncio.to_thread`.** `smtplib` blocks until the mail server answers. Called from a
coroutine, it would stop the whole event loop for that long, chat answers included.
`to_thread` runs the function on a worker thread and lets the coroutine wait for it with the
loop free. The digest sends without it, after its loop has finished; `mail.py` tells both
halves of that. The mail stand-in in the tests refuses a connection made from a thread whose
loop is running, so a send moved back onto the loop fails every test of the flow.

**`except Exception`, on purpose.** Everywhere else this project catches the errors it expects
and lets the rest surface as bugs. The three optional steps here catch `Exception`, because
"may fail" has to mean in any way: OpenAI answering 200 with a page of HTML makes its client
raise an `AttributeError`, and nobody lists that. Two things keep it honest. The type of the
error is logged, so a bug still shows. And `asyncio.CancelledError` is a `BaseException` and
not an `Exception`, so shutdown and the deadlines still cut a step off where it stands.

**A thread cannot be stopped.** `asyncio.timeout` around `asyncio.to_thread(...)` cancels the
waiting and leaves the thread running to the end of its function. There is no way to interrupt
it from outside. The only lever is inside: give the blocking call a timeout of its own, which
is why `mail.send` takes one.

**`next()` with a default.** `next((why for holds, why in refusals if holds), None)` is the
first item of a generator, or `None` when there is none. It reads as "the first refusal that
holds" and replaces five `if ... return` pairs.

**`hmac` with a key of the process's own.** `hmac.new(key, text, hashlib.sha256)` is a hash
that only the holder of the key can reproduce. A plain SHA-256 of an email address is not much
of a disguise, because anybody can hash a list of likely addresses and compare. The key is
`secrets.token_bytes(32)`, made when the process starts, and that is enough: the state it
protects lives no longer than the process.

**`asyncio.gather` for two waits that share one allowance.** At shutdown the chat's answers and
the form's messages each get ten seconds. Awaited one after the other they could take twenty.
`gather` runs both waits at once, so the total is ten.

**Raw strings for patterns that name characters.** `_SPACE` is written `r"\t\n ... "` with the
escapes left as escapes. The regular expression engine understands them itself. In an ordinary
string Python would turn each into the invisible character it names before the engine saw it,
and the source file would hold characters no reviewer can see.

**`re.ASCII`, and `fullmatch`.** Covered above. Both are cases of Python's defaults being wider
than JavaScript's.

**A `type` alias, and reading its members.** `type Reason = Literal["project", ...]` is Python
3.12's alias statement. The alias is an object with the real type in `__value__`, so
`get_args(Reason.__value__)` gives the five strings, and `REASONS` is built from that. One
list, used by the type checker and at run time.

**`dataclasses.replace`.** A frozen dataclass cannot be changed, so "the same mailbox with a
different recipient" is a copy: `dataclasses.replace(owner, to=visitor_address)`.

**`collections.deque` and `time.monotonic`.** The hour's arrivals leave from the front as new
ones join the back, and a list is slow at the front. `monotonic` only goes forwards, so a
system clock being put right cannot make an hour longer or shorter.

**`str.format` with a visitor's text as a value.** The brief is the template and the message is
an argument: `brief.format(message=...)`. Braces in a message mean nothing, because only the
template is read for fields. An f-string or a `%` would be the same; what would not be is
formatting a string that already holds the visitor's text.

**`EmailMessage.add_alternative`.** `set_content(text)` makes a plain message.
`add_alternative(html, subtype="html")` turns it into `multipart/alternative`: two versions of
one thing, of which a mail program shows the last it can.

## How it is tested

- **`tests/unit/test_contact_rules.py`**: the parity test, one case per submission in the
  fixture, 81 of them. Also that the fixture covers every outcome, that it holds no character outside ASCII
  that a reviewer could not see, and the one deliberate difference from JavaScript.
- **`tests/unit/test_contact_parts.py`**: the pieces by themselves. The classifier is shown the
  submission in the workflow's exact words; a verdict is clamped and clipped; each brief is
  filled; braces in a message stay braces; every fault of a reply, with the numbers written
  out on each side of each limit; which addresses are safe to write to; the acknowledgement's
  fixed frame; each notification's subject; every character Python counts as a line break,
  put into a subject and the subject into a real header; the desk's count, its refusal, and
  work that goes on when whoever waited for it is cancelled.
- **`tests/unit/test_contact_service.py`**: the whole flow, with `tests/openai_fake.py` and
  `tests/smtp_fake.py` behind it. Every branch of the diagram; each way of failing, with the
  notification still sent, an error nobody listed included; a model that never answers and an
  email that never goes, with the deadlines shortened; a reply the checks refuse, with nothing
  sent to the visitor; nothing written as markup; and **a recorder on the log, searched for
  the address, the subject, the message, the explanation and the reply**.
- **The same file reads what a mail server would be given.** The stand-in runs `smtplib`'s own
  `send_message`, which turns a message into an envelope and bytes, and keeps both
  (`server.delivered`). An address written as an encoded word reaches the owner alone. A
  subject written as one is on the wire as the text it was typed as, with exactly the headers
  the message should have.
- **`tests/unit/test_contact_replies.py`**: the two limits, with the clock handed in so that a
  day passes in a line: a minute short of 24 hours and exactly 24, the tenth reply and the
  eleventh, nought, a refusal that uses nothing up, pruning, the ways of writing one inbox, and
  a check that no address is held as itself.
- **`tests/unit/test_contact_api.py`**: the endpoint in process, with the flow replaced. The
  key; a body that does not fit; the limit and its `Retry-After`; each error's status, with
  nothing of the error in the response; a caller that hangs up; and shutdown.
- **`tests/unit/test_contact_cli.py`**: `try` prints both emails, says why there would be no
  acknowledgement, and never touches the mail stand-in.
- **`tests/unit/test_prompts.py`**: the eight prompts are pinned by version and hash like the
  rest, and compared line by line with n8n's own text where the export is on disk. It skips
  in CI, which does not have the export.
- **`tests/unit/test_mail.py`**: the module the digest already used, and what this flow added:
  a name, a `Reply-To`, an HTML version, and a header with a line break in it refused before
  any server is reached.
- **In the portfolio**, `api/tests/contact.test.js`: what is forwarded and with which key; what
  each of this API's answers becomes; the same limits, counted the same way; a redirect not
  followed; nothing typed in the log.

**Each behaviour here was broken on purpose to see a test fail.** One change to the code at
a time, made in memory: a deadline removed, a threshold moved by one, an escape left out, a
check on a header dropped. Each has to turn at least one test red. The portfolio's route had
the same done to a scratch copy. The first runs found tests that measured a limit against its
own constant and would have passed at any value.

**No unit test can reach a mail server.** `tests/unit/conftest.py` replaces `smtplib.SMTP` for
every test with something that fails it, and a test that sends installs the stand-in over
that. The guard exists because of what happened without it: the first test of this flow that
left the stand-in out opened a connection to the real mail server named in the default
settings, with a made-up account. The server hung up and nothing was sent. A unit test that
can reach the network passes or fails on the weather, and one day sends something.

What no test covers is the real thing: OpenAI's actual answers and a real mailbox. The first is
what `try` is for. The second is the form itself, after a deploy.

## Going live

Switched on 2026-10-14. [DEPLOYMENT.md](DEPLOYMENT.md) §11, "The contact form", has the setting
and how to check it, and §12 the runbook of the switch from n8n, with which of its steps were
reported done, and its rollback: this service first, then the portfolio's API, a message through
the form, then the workflow deactivated.
