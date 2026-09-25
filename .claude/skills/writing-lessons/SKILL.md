---
name: writing-lessons
description: How to write and revise the teaching lessons in docs/lessons/, which explain Python to an experienced developer who is new to the language. Use this whenever creating a new lesson file, revising an existing one, or responding to feedback that a lesson was confusing, too technical, or jumped around. Also use it when reviewing the reader's answers to a lesson's exercise, and when explaining any Python concept in conversation as part of this project, since the same register and structure apply there. Reach for it even if the request just says "write lesson 5" or "explain decorators" without mentioning teaching or lessons.
---

# Writing the lessons

The lessons in `docs/lessons/` teach Python by building this project one piece at a time. Each
one lands real code, explains every Python-specific decision in it, and ends with an exercise
done against the real repository. Nothing in them is a toy.

This skill is about how to write them well. The structure is the easy part; the register and the
one-idea discipline are what actually decide whether a lesson lands.

## Who you are writing for

One experienced developer who is new to Python. They work in PHP (Laravel, CodeIgniter), Node,
Vue and Nuxt, run their own Docker infrastructure, and have shipped production systems for years.

That background sets the level precisely:

- **Do not explain programming.** Functions, tests, classes, HTTP, SQL, Docker — all known. A
  paragraph explaining what a decorator "is like a wrapper" wastes their time and slightly insults
  them.
- **Do explain what is Python-specific.** Why the import system behaves as it does, why there is
  no `private`, why async is cooperative rather than threaded, why packaging looks the way it
  does. These are the things their existing instincts will actively mislead them about.
- **Their instincts from PHP and Node are the main hazard.** When Python differs from what those
  languages taught them, say so explicitly. That is where the misunderstandings come from, and a
  sentence naming the difference saves an hour of confusion.

The lessons are committed to a public repository, so a stranger learning the same things should
be able to read them. That rules out inside references, but it does not mean writing formally.

## The one-idea rule

This is the most important thing in this skill, and the thing most likely to go wrong.

**Find the single idea the whole lesson rests on. State it once, plainly. Then derive everything
else from it.**

A lesson that presents three related mechanisms as three separate explanations teaches the reader
that they are three separate things. They will then mix them up — and reasonably so, because the
structure of the writing told them the mechanisms were interchangeable.

This happened in lesson 2 and is worth studying, because the failure was invisible until the
reader answered the questions.

> **What went wrong.** The lesson explained `sys.path`, then the `src/` layout, then absolute
> imports — three headings, three explanations, each correct. The reader then used the `src/`
> layout to explain why `logging.py` does not shadow the standard library, which is wrong. But it
> was a reasonable inference: the lesson had presented the mechanisms side by side, so of course
> they looked connected.
>
> **The fix.** One rule, stated once: *Python keeps a list of folders, looks only at what is lying
> directly in each, and stops at the first match.* Then the three confusing things were worked
> through in a row as consequences of it. Same content, a third fewer words, and the reader can
> derive the answers instead of recalling them.

Before writing, ask: what is the one sentence that makes all of this obvious? If you cannot find
it, you do not understand the topic well enough to teach it yet, and more headings will not help.

A good sign you have found it: you can answer every "check yourself" question by pointing at that
one sentence.

## Register

Write like a person explaining something to a colleague. Not like reference documentation.

The specific failure to avoid is **documentation voice**: a heading every four lines, dense
bullets, bold scattered everywhere, and sentences that each state a fact without connecting to
the one before. It looks thorough and is genuinely hard to learn from, because the reader has to
assemble the argument themselves.

What to do instead:

- **Write in paragraphs that follow on from each other.** Each sentence should pick up where the
  last one left off. If you can reorder two sentences without noticing, they are not connected.
- **Use bullets for things that really are lists** — three alternatives, four config values. Not
  for an explanation that has been chopped up.
- **Let one heading cover a whole idea**, even if that is six paragraphs. Headings are for
  navigation, not pacing.
- **Bold the occasional sentence that a reader should leave with.** If there are five bold spans
  on a screen, none of them mean anything.
- **Carry an analogy through** rather than dropping it after one line. If you introduce drawers
  and boxes, still be talking about drawers and boxes three paragraphs later.
- **Contractions and plain words are fine.** "It turns out to be fine" beats "this is not
  problematic".

Compare. The same content, first as documentation and then as writing:

> **Documentation voice** — technically complete, hard to learn from:
>
> ### How `import` resolves
> When you write `import openai`, Python walks `sys.path` in order and takes the **first** match.
> `sys.path` is built from:
> 1. `sys.path[0]` — the directory of the script being run, or the current working directory
> 2. anything in `PYTHONPATH`
> 3. the standard library
> 4. `site-packages`
>
> Entry 1 is the one that causes trouble.

> **Written for a person** — same facts, derivable:
>
> ### How Python finds things
> When you write `import openai`, Python has to go and look for it, and the way it searches is
> simpler than you would expect. It keeps a list of folders. It works down that list from the top,
> checking each folder as it goes, and takes the first thing it finds with the right name. Then it
> stops looking.
>
> There is one detail in there that matters far more than it sounds like it should. Python only
> looks at what is sitting *directly* in each folder. It never goes digging through subfolders.
>
> Picture a row of drawers…

The second is longer per fact and much shorter overall, because it does not need to repeat itself.

## Shape of a lesson

Seven sections, in this order. `assets/lesson-template.md` has the skeleton.

1. **What you'll understand** — three to five bullets, written as things they will be able to
   explain afterwards, not topics that will be covered.
2. **Why it matters here** — tie it to this project specifically. Which upcoming lesson depends on
   it, which trap it prevents. Generic motivation ("configuration is important") is filler.
3. **The concepts** — the teaching. Built on the one idea. Most of the words live here.
4. **The code** — a walkthrough of what was actually written, decision by decision. Explain the
   choices, not the syntax. If a file exists, this section must account for why every part of it
   is there.
5. **Coming from PHP / Node** — a short comparison table, then one or two differences that
   genuinely bite. The table is the warm-up; the prose underneath is the value.
6. **Exercise** — see below.
7. **Check yourself** — five or six questions, answers in a `<details>` block at the bottom so
   they can be avoided until wanted.

Deviate when a topic needs it. A lesson with nothing interesting to say about PHP can drop that
section rather than padding it.

## Exercises

The exercise is where the learning actually happens, so it is worth more thought than it looks.

**Make it real.** The best exercises implement something the project genuinely needs, or produce
a diagnostic the reader will meet again. Writing the validator that rejects a `DATABASE_URL` on
the wrong port teaches Pydantic *and* lands a real guardrail. Breaking an import on purpose
teaches an error message they will see for the rest of their career.

**Ask for a prediction before the command.** "Predict what this does, then run it" is worth far
more than "run this and observe". The prediction is what surfaces a wrong mental model — to the
reader, not just to you.

**Keep it to 15–30 minutes** and have it end with the repository clean, so the exercise cannot
silently leave debris behind.

**Ask for reasoning, not just output.** "Why is this in the dev group rather than the main
dependencies, and what would break in the Docker image if it were in the wrong place?" tells you
whether they understood. "Run this command" does not.

## Verify before you write

Run every command and error message the lesson claims, before writing it down. This matters more
than usual here because the exercises ask the reader to predict outcomes — a lesson that predicts
wrong teaches the wrong thing and destroys trust in everything around it.

This is not theoretical. Writing lesson 2 involved actually creating the circular import, actually
creating the shadowing file, and actually inspecting the built wheel. One of those produced a
detail worth teaching that would not have been obvious from reasoning alone.

Paste real output into the lesson rather than approximating it. Real paths and real error text are
more convincing and stop the reader wondering whether their own output is wrong.

## Reviewing the reader's answers

When they answer the "check yourself" questions, the review is part of the teaching.

**Score honestly.** "Two right, one half-right, three off" is more useful than encouragement.
They are an experienced developer and can take it; softening it wastes the signal.

**Look for the common cause first.** Several wrong answers usually share one misunderstanding.
Fixing that is worth more than six separate corrections, and it tells you the lesson has a
structural problem rather than a coverage problem.

**Prove it, do not assert it.** If they have the wrong model, run something that shows the right
one and paste the output. A demonstration settles it; a restatement of the original explanation
just repeats whatever failed the first time.

**When they say they still do not understand, that is a fact about the writing.** Do not re-explain
in the same shape, and do not add more detail — the problem is almost never missing information.
Find the one idea, rebuild around it, and say plainly that the explanation was the problem.

## Constraints

- **The repository is public.** No IP addresses, no hostnames beyond ones already public, no real
  credentials in examples, no personal email addresses beyond Mihail's two published ones. The same
  rules that apply to code apply to lesson text.
- **Keep the index current.** `docs/lessons/README.md` lists every lesson and tracks progress;
  update it in the same commit.
- **One lesson per commit**, with a message explaining what it teaches and why — the commits are
  public too.
- **Do not invent future content.** If a lesson wants to reference something a later lesson will
  build, say that it is coming rather than writing it early.
