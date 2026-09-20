# Lesson 2 — Packages, modules and imports

## What you'll understand

- How Python actually resolves `import x`, and why `sys.path[0]` is the most dangerous entry in it
- What `__init__.py` does, and how it doubles as a package's public API
- The specific class of bug the `src/` layout exists to prevent
- Why `portfolio_ai/logging.py` will not shadow the standard library, even though it looks like it must
- What a circular import looks like, and the error it produces

## Why it matters here

Import bugs in Python rarely announce themselves as import bugs. They surface as a module that
mysteriously has no attributes, a standard library function behaving oddly, or — worst — code
that works perfectly on your machine and fails inside the container.

Two of those are waiting in this project specifically. Lesson 4 creates a file called
`logging.py` inside the package. Lesson 10 builds a wheel and installs it somewhere that is not
your repository. Both are fine, and both would be alarming if you did not know why.

## The concepts

### Two words, then the interesting part

Python has two words for the things you import, and the difference between them is smaller than
it sounds.

A **module** is a `.py` file. A **package** is a folder with an `__init__.py` inside it. That is
genuinely the whole distinction — the `__init__.py` is how you tell Python that this folder is
something you can import, rather than just a folder that happens to have Python files in it.

```
portfolio_ai/            a package        import portfolio_ai
├── __init__.py          makes it one
├── exceptions.py        a module         import portfolio_ai.exceptions
└── db/                  also a package   import portfolio_ai.db
    └── __init__.py
```

Modern Python will sometimes treat a folder *without* an `__init__.py` as importable too, but we
do not rely on that. If you rely on it, a mistyped folder name quietly becomes a valid empty
package instead of an error, and you spend an afternoon wondering why your module has nothing in
it. Writing the file out is cheap insurance.

With the vocabulary out of the way, the next section is the one that actually matters.

### How Python finds things

When you write `import openai`, Python has to go and look for it, and the way it searches is
simpler than you would expect. It keeps a list of folders. It works down that list from the top,
checking each folder as it goes, and takes the first thing it finds with the right name. Then it
stops looking.

There is one detail in there that matters far more than it sounds like it should. Python only
looks at what is sitting *directly* in each folder. It never goes digging through subfolders.

Picture a row of drawers. You open each one in turn and glance at what is lying loose inside, and
the moment you spot what you came for, you stop. But if something has been packed away in a box
at the bottom of a drawer, you walk straight past it. You would have to already know the box was
there, and open it by name.

A Python package is that box. Our `portfolio_ai` folder is a box sitting in the `src` drawer, so
nothing inside it can be found by a plain `import`. To reach anything in there you have to name
the box first, which is what `portfolio_ai.something` is doing.

Here is the real list for this project, with the less interesting entries left out:

```
0.  <the folder you are standing in>
3.  ...\Python312\Lib                          the standard library
6.  ...\ai_assistant\.venv\Lib\site-packages   everything we installed
7.  ...\ai_assistant\src                       added by the editable install
```

You can print it yourself at any time:

```bash
uv run python -c "import sys; [print(i, p or '<cwd>') for i, p in enumerate(sys.path)]"
```

The first entry is the one worth pausing on, because it is not the project root or any fixed
place — it is wherever your terminal happens to be standing at that moment. Move to a different
folder and that entry moves with you. A surprising number of baffling import problems come back
to that single line.

### The same rule, three times

That is the whole mechanism. Everything else in this lesson is just that rule applied to
different situations, so let us walk through the three that matter here.

**Your own file beating a real library.** Suppose there is a file called `openai.py` in the folder
you are working in. You write `import openai`, and Python starts where it always starts: the
folder you are standing in. Your file is lying right there, so it wins, and Python never gets far
enough down the list to reach the real library in `site-packages`. Run the same command from a
different folder and it behaves completely differently, because the first entry in the list has
changed underneath you. You will do this deliberately in the exercise.

**A file called `logging.py` that does not break `logging`.** In lesson 4 we are going to add a
file called `logging.py` inside our package, and that looks like it must cause trouble, because
Python already has a `logging` module of its own. It turns out to be completely fine, and the
rule explains why.

When code inside our package says `import logging`, Python goes down the list as usual. The
folder you are standing in has no `logging.py`. Next comes the standard library, which does have
one, so Python takes it and stops. Our file is never even considered.

The reason is the box again. What the list actually contains is the `src` folder, and the only
thing lying directly inside `src` is `portfolio_ai`. Our `logging.py` is tucked inside that, one
level deeper than Python ever looks. You can see both halves of that for yourself:

```bash
ls src/
# portfolio_ai   -- that is genuinely all that is in the drawer

uv run python -c "import sys, pathlib, portfolio_ai
pkg = pathlib.Path(portfolio_ai.__file__).parent
print('is the package folder on the list?', str(pkg) in sys.path)"
# False
```

Which gives us the sentence worth remembering: a package's own folder is never on the list, only
its parent is. That is why our file is reachable only by naming the box.

```python
import logging                    # the standard library
from portfolio_ai import logging  # ours, by naming the box
from . import logging             # ours, the shorthand for "the box I am already in"
```

If the instinct that this *should* break feels strong, there is a good reason for it. Python 2
really did peek at neighbouring files first, and a file like ours would have hidden the standard
library across the whole package. It caused enough confusion that the behaviour was removed.

**Why the code sits in a `src` folder at all.** Plenty of projects put the package straight into
the project root, and we deliberately did not. This is the payoff.

```
what most projects do                 what we do

ai_assistant/                         ai_assistant/
├── portfolio_ai/                     ├── src/
└── pyproject.toml                    │   └── portfolio_ai/
                                      └── pyproject.toml
```

Imagine we had gone with the layout on the left. You are standing in the project root, so that is
the first folder on the list, and `portfolio_ai` is lying right there in it. Python finds it
straight away. That sounds convenient, and it is, right up until you notice what it means: your
code works whether or not the package was ever properly installed.

That is the part that causes trouble later. You would be running against the folder on your disk
rather than against the thing you actually ship to the server. If an `__init__.py` is missing, or
a file never made it into the built package, or you forgot to declare where the prompt files
live, none of it shows up — because you never went through an installation for it to show up in.
You find out when the container starts.

With the layout on the right, the project root contains `src`, `docs` and `pyproject.toml`, but
no `portfolio_ai`. Python does not find it in the first folder. It finds it further down the
list, in `src`, which is only on the list because the install put it there. So the only route
that works is the installed one, which means every time you run anything locally you are
exercising the same path the server will. Packaging mistakes fail on your machine, on the day you
make them.

That is also why lesson 1 ended by building the package and looking inside it rather than taking
it on trust.

> Worth keeping hold of: Python looks through a list of folders, only at what is lying directly
> in each one, and a package's own folder is never on that list.

### Two ways to write the same import

Once you are inside a package there are two ways to refer to something else in it, and they do
the same job:

```python
from portfolio_ai.exceptions import ConfigError   # the full name
from .exceptions import ConfigError               # "next to me"
from ..db.pool import get_pool                    # "up one, then into db"
```

The dots are shorthand for "the box I am already in", which is why they get shorter as you get
closer. One dot means this package, two means the one above it, and so on.

Each style has a genuine argument behind it. The short form keeps working if you ever rename the
package, and it makes it obvious at a glance that you are reaching for something internal. The
full form reads the same wherever you meet it, including in a test file that lives outside the
package entirely, and you can search the codebase for it and find every use.

We use the full form everywhere, including inside `__init__.py`. Not because it is clearly
better, but because having one rule with no exceptions is easier to follow than a better rule you
have to think about each time. The price is a find-and-replace if we ever rename the package,
which is a single afternoon weighed against a decision you would otherwise make daily.

One thing to know about the short form before you meet it in someone else's code: the dots count
packages, not folders on disk. Use `from ..x import y` in a module that has no parent package and
you get `attempted relative import beyond top-level package`, which sounds like a broken path but
is really Python saying there is nothing above you to go up into.

### What `__init__.py` is for

`__init__.py` runs the first time anyone imports the package. Whatever it defines, or imports,
becomes part of the package itself — which is what lets this work:

```python
from portfolio_ai import ConfigError
```

even though `ConfigError` is actually defined over in `exceptions.py`. The `__init__.py` imported
it, so it now belongs to the package too. That is all a re-export is.

It is worth being careful about what you put there, though, because that file runs on *every*
import of the package. Even `import portfolio_ai.db.pool`, which has nothing to do with it, runs
`__init__.py` first. So anything slow you import there is paid for by everything: every command
you run, every test, every time the app boots. And if what you import happens to import the
package back, you have built yourself a loop — which is the next section.

That is why we only re-export from `exceptions.py`. It imports nothing at all, so it cannot be
slow and cannot loop.

### What `__all__` does, and what it does not

Underneath the re-exports there is a list:

```python
__all__ = ["ConfigError", "PortfolioAIError", "PurgeSafetyError", "__version__"]
```

It looks like an access control list. It is not one. It does exactly two things.

The first is that it decides what `from portfolio_ai import *` brings in. That is the textbook
answer, and honestly the less useful one, because almost nobody should be writing `import *` in
real code.

The second matters more: it is how you tell the rest of your tooling what counts as the public
part of this package. Documentation generators build their pages from it. Linters use it to
decide whether an unused import is a mistake or a deliberate re-export. It is a note to everyone
downstream saying "these are the names I intend people to use".

What it emphatically does not do is stop anyone reaching the rest:

```python
from portfolio_ai import exceptions   # not in __all__, imports perfectly fine
```

This surprises people coming from languages where `private` is enforced by the compiler. Python
has nothing like that. A leading underscore on a name, or leaving something out of `__all__`, is
a sign on a door, not a lock. Think of it as documentation with just enough teeth that your
tooling pays attention.

### When two files need each other

Sooner or later two modules end up importing each other, and the error you get is not especially
forthcoming the first time:

```
ImportError: cannot import name 'B' from partially initialized module 'b'
(most likely due to a circular import)
```

"Partially initialized" is the part that explains it. Importing a file means running it, from the
top down. So if `a` asks for `b` on its very first line, Python pauses `a` and starts running
`b` — and if `b` then asks for something from `a`, it is asking a file that has run exactly one
line so far and has not defined anything yet. There is nothing there to hand back.

There are three ways out, and they are worth knowing in order, because the first is usually the
right one.

Most often the loop is telling you something true: the two files each know too much about the
other, and whatever they are both reaching for belongs in a third file they can both import. That
is a design fix rather than an import fix, and it tends to leave the code better than it found it.

When that is not practical, you can move the import inside the function that needs it. By the
time anyone calls that function, both files have finished loading, so the problem disappears. It
is cheap and it works, with the mild cost that the dependency is no longer visible at the top of
the file.

The third case is when you only needed the import for a type annotation in the first place, which
has its own tidy solution that lesson 9 will get to.

You will produce this error on purpose in the exercise, because recognising it on sight is worth
more than understanding it in the abstract.

### `py.typed`

An empty file that changes how other people's tools treat your code.

When a type checker looks at an installed package, it assumes by default that the package has no
type information, and treats everything it exports as "could be anything". It does that even if
the source is full of perfectly good annotations — it has no way to know whether they were meant
seriously.

Shipping a file called `py.typed` inside the package is how you say they were. That is the entire
mechanism: the file is empty, and its existence is the message.

Without it, the typing work in lesson 9 would only count inside this repository and would quietly
stop mattering the moment the package was installed anywhere else.

### Finding files that are not code

Not everything in a package is Python. Later on, the long prompts we are porting over from n8n
will live in `assistant/prompts/` as Markdown files, shipped inside the package alongside the
code that uses them. Which raises a question worth answering now: how does the code find them
once it is running?

The obvious answer is to work it out from where the current file is:

```python
path = Path(__file__).parent / "prompts" / "rag_agent.md"
```

That works, right up until it does not. It assumes the package is sitting on disk as ordinary
folders and files, and packages are not always unpacked that way — some get loaded straight out
of a zip, for instance. When that happens there is no folder to look next to, and the code breaks
somewhere far from anything you changed.

The reliable way is to ask the import system, since it already knows where the package ended up:

```python
from importlib.resources import files

text = (files("portfolio_ai.assistant") / "prompts" / "rag_agent.md").read_text(encoding="utf-8")
```

It reads almost the same, and it keeps working whatever shape the package is in. Mentioned here
because it belongs with everything else about how packages are put together; we will actually use
it once there are prompts to load.

## The code

### `exceptions.py`

A root class and two concrete errors:

```python
class PortfolioAIError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(PortfolioAIError): ...
class PurgeSafetyError(PortfolioAIError): ...
```

The root class earns its place by making this distinction possible:

```python
try:
    run_ingestion()
except PortfolioAIError:
    ...   # we refused, on purpose, for a reportable reason
```

`TypeError` and `KeyError` are not caught by that clause, so genuine bugs still propagate and
still get a stack trace. Catching bare `Exception` would swallow them, which is how a crash
becomes a silent wrong answer.

Only two subclasses, both with imminent users: `ConfigError` for lesson 3, and `PurgeSafetyError`
for the ingestion guard documented in ARCHITECTURE.md §7. Inventing the other eight now would be
guessing at names for code that does not exist, and those guesses age badly.

`PurgeSafetyError` carries structured data rather than just a string:

```python
def __init__(self, found: int, minimum: int) -> None:
    self.found = found
    self.minimum = minimum
    super().__init__(f"Discovery returned {found} documents, below the safety floor of {minimum}. ...")
```

An exception is an ordinary object, so it can hold whatever the handler needs. Here a caller can
log `err.found` without parsing it back out of the message. Calling `super().__init__(message)` is
what makes `str(err)` return that text — skip it and your exception prints as empty.

Note the module docstring's last paragraph: **this module imports nothing**. Since everything
else imports *it*, any dependency it picked up would become a dependency of the whole codebase
and a candidate for a cycle. Leaf modules should stay leaves.

### `__init__.py`

A package docstring naming each subpackage, the version, three re-exports and `__all__`:

```python
from portfolio_ai.exceptions import ConfigError, PortfolioAIError, PurgeSafetyError

__version__ = "0.1.0"

__all__ = ["ConfigError", "PortfolioAIError", "PurgeSafetyError", "__version__"]
```

Absolute import, per the convention above, even though this file is *inside* the package it is
importing from. That works because by the time the `from` line runs, `portfolio_ai` is already
registered in `sys.modules` as a partially initialized module, and `portfolio_ai.exceptions` is a
fresh submodule import that does not need the parent to be finished.

### The subpackage skeleton

Eight directories, each with an `__init__.py` holding a one-line docstring:

```python
"""Database access. Every SQL statement in the project lives in this package."""
```

They are empty of code but not empty of meaning — the tree now documents itself, and matches
ARCHITECTURE.md §6 exactly. Creating them all now rather than one per lesson means the shape of
the system is visible from the first `ls`.

### `py.typed`

Zero bytes. Its existence is the signal.

## Coming from PHP / Node

| | Python | Node | PHP |
|---|---|---|---|
| Unit of code | module (`.py` file) | module (file) | file + class |
| Grouping | package (dir + `__init__.py`) | directory | namespace |
| Resolution | `sys.path`, first match wins | `node_modules` walk up the tree | PSR-4 autoload map |
| Re-export | `__init__.py` | `index.js` | — |
| Public surface | `__all__` + naming convention | `exports` field | `public` / `private` |

Two differences that will bite if you carry your instincts over.

**Namespaces are not directories.** In PHP, `namespace App\Services;` is a declaration inside the
file, and PSR-4 maps it to a path by convention — you could put the file anywhere and fix the
autoloader. In Python the package name *is* the directory name. Rename the directory and you have
renamed the package.

**There is no `index.js` resolution.** Node lets `require('./utils')` find `utils/index.js`
automatically. Python has no equivalent: `import portfolio_ai.db` runs `db/__init__.py` because
that file *is* the package, not because it is a conventional entry point being resolved to.

And the one that catches everyone: **privacy is a convention.** PHP's `private` is enforced by the
engine. Python's leading underscore is enforced by nothing at all — it is a note to the reader and
a signal to linters. `__all__` is documentation, not a wall.

## Exercise

Predict the outcome of each step **before** running it. Writing the prediction down first is the
point; the running is just marking your own work.

### Part 1 — shadowing

1. At the repository root, create `openai.py` containing:

   ```python
   raise RuntimeError("this is not the openai you are looking for")
   ```

2. Predict: what does `uv run python -c "import openai"` do from the repository root?

3. Run it. Then run the same command from a different directory:

   ```bash
   cd .. && uv run --project ai_assistant python -c "import openai; print(openai.__file__)"
   ```

4. Predict before running: why do the two differ? Which `sys.path` entry is responsible?

5. Delete `openai.py`. Confirm `git status` is clean.

### Part 2 — circular imports

1. Create two throwaway modules inside the package:

   ```python
   # src/portfolio_ai/a.py
   from portfolio_ai.b import thing_b
   thing_a = "a"

   # src/portfolio_ai/b.py
   from portfolio_ai.a import thing_a
   thing_b = "b"
   ```

2. Predict the **exact exception type**, and which of the two modules the message will name, for
   `uv run python -c "import portfolio_ai.a"`.

3. Run it. Were you right about which module it blamed? Now predict what changes for
   `import portfolio_ai.b` — then check.

4. Fix it using option 2 from the concepts section (move the import inside a function). Explain in
   one sentence why that resolves the cycle when the top-level import could not.

5. Delete both files. Confirm `git status` is clean.

## Check yourself

1. Why does a file named `openai.py` in your working directory beat the installed `openai` package?
2. `portfolio_ai/logging.py` is coming in lesson 4. Why will `import logging` inside
   `portfolio_ai/config.py` still get the standard library?
3. What breaks if you put a heavy import in `__init__.py`?
4. What does `__all__` actually control — and what does it *not*?
5. The `src/` layout means you cannot import your own package without installing it. That sounds
   like pure friction. What does it buy?
6. Why does `py.typed` need to exist when the type hints are already in the source?

---

<details>
<summary>Answers</summary>

1. The folder you are standing in is the first one on Python's list, ahead of both the standard
   library and everything you installed. Python stops at the first match, so your file wins and
   the real library never gets looked at. Stand somewhere else and the answer changes, because
   that first entry moves with you.

2. Because Python only looks at what is lying directly in each folder on its list, and our
   package's folder is not on that list — only its parent, `src`, is. The one thing lying
   directly in `src` is `portfolio_ai`, so `logging.py` is a level too deep for a plain `import`
   to see. To reach it you have to name the box: `from portfolio_ai import logging`. (Python 2
   did check neighbouring files first, which is where the worry comes from, but that behaviour is
   long gone.)

3. That file runs on *every* import of the package, even ones that have nothing to do with it,
   like `import portfolio_ai.db.pool`. So anything slow in there is paid for by everything — every
   command, every test run, every boot. And if the thing you import turns out to import the
   package back, you have made a loop.

4. Two things: it decides what `from package import *` brings in, and it tells documentation
   tools and linters which names you consider public. It does not restrict anything — whatever
   you leave out is still importable by name. A sign on a door, not a lock.

5. It means the only way your code can import your package is through the installed copy, which
   is the same route the server uses. So if something is missing from the built package, you find
   out on your own machine the day you break it, rather than when a container fails to start.
   The friction is the whole point.

6. Because a type checker looking at an installed package assumes there is no type information in
   it and treats everything as "could be anything" — it has no way to know your annotations were
   meant seriously. The empty `py.typed` file is how you tell it they were. Without it, all the
   typing work in lesson 9 would stop counting the moment the package was installed elsewhere.

</details>
