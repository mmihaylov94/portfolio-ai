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

### Modules, packages, and the file system

A **module** is a `.py` file. A **package** is a directory containing `__init__.py`. That is the
whole distinction — packages are directories that Python has been told to treat as importable.

```
portfolio_ai/            package        import portfolio_ai
├── __init__.py          runs on import of the package itself
├── exceptions.py        module         import portfolio_ai.exceptions
└── db/                  subpackage     import portfolio_ai.db
    └── __init__.py
```

Since Python 3.3 a directory *without* `__init__.py` can also be importable — a **namespace
package** (PEP 420). They exist to let a single package span multiple directories, which is
useful for plugin systems and almost nothing else. We use explicit `__init__.py` files because
the implicit behaviour hides mistakes: a typo'd directory name becomes a valid empty namespace
package rather than an error.

### How `import` resolves

When you write `import openai`, Python walks `sys.path` in order and takes the **first** match.
`sys.path` is built from:

1. `sys.path[0]` — the directory of the script being run, or **the current working directory**
   for `python -c`, `python -m`, and the REPL
2. anything in the `PYTHONPATH` environment variable
3. the standard library
4. `site-packages` — where installed packages live, including anything a `.pth` file points to

Entry 1 is the one that causes trouble. Verify it:

```bash
uv run python -c "import sys; print(repr(sys.path[0]))"
# ''  -- the empty string means "current directory"
```

Your working directory sits ahead of the standard library and ahead of `site-packages`. A file
called `openai.py` in the directory you happen to be standing in wins against the real `openai`
package. You get a confusing `AttributeError` rather than anything resembling "you shadowed a
dependency". That is the first half of your exercise.

### Why the code lives under `src/`

This is the part worth internalising, because the reasoning is not obvious and the payoff is
invisible when it works.

Suppose the package sat at the repository root instead:

```
ai_assistant/
├── portfolio_ai/        <- package here
└── pyproject.toml
```

Run anything from the repository root and `sys.path[0]` is that root, so `import portfolio_ai`
finds the directory and succeeds — **whether or not the package was ever installed**. That sounds
convenient. It means:

- your tests pass against the source tree, not against the thing you actually ship
- a missing `__init__.py`, a file excluded from the wheel, or package data that was never
  declared all stay invisible
- you discover the problem when the container starts, which is the worst possible moment

With `src/`, the repository root contains no importable package. The *only* way
`import portfolio_ai` can succeed is through the installed package — which, in development, is
the editable install from lesson 1. So every local run exercises the real installation path, and
a packaging mistake fails immediately instead of at deploy time.

A fair objection: the editable install *does* put `src` on `sys.path` via that `.pth` file, so
the directory is reachable either way. True — the difference is that it is reachable by the
sanctioned route rather than by accident of where you were standing. Delete the install and
`import portfolio_ai` stops working, which is exactly the feedback you want.

You can watch this happen in the verification step at the end of the lesson.

### `logging.py` will not shadow the standard library

Lesson 4 creates `src/portfolio_ai/logging.py`. Every instinct says this must break `import
logging` for the rest of the package. It does not, and the reason is historical.

In Python 2, imports were *implicitly relative*: a module inside a package would find its sibling
first, so `portfolio_ai/logging.py` really would have shadowed the standard library everywhere
inside `portfolio_ai/`. This caused enormous confusion and PEP 328 removed it.

In Python 3 all imports are absolute by default. Inside `portfolio_ai/config.py`:

```python
import logging                    # the standard library, unambiguously
from portfolio_ai import logging  # our module, if we ever wanted it
from . import logging             # our module, explicit relative form
```

So a package-internal module may share a name with a standard library one without incident. A
*top-level* module still cannot — a `logging.py` at your repository root would shadow the real
one, because of `sys.path[0]` again.

### Absolute and relative imports

Both work inside a package:

```python
from portfolio_ai.exceptions import ConfigError   # absolute
from .exceptions import ConfigError               # relative
from ..db.pool import get_pool                    # relative, one level up
```

Relative imports survive renaming the package and make it obvious that something is internal.
Absolute imports are greppable, unambiguous when read in isolation, and identical whether they
appear in this package or a test.

**This project uses absolute imports everywhere, including inside `__init__.py`.** One rule with
no exceptions is easier to apply than a good rule with a carve-out. The cost is that renaming the
package means a find-and-replace, which is a one-off against a benefit you get every day.

Relative imports also have a hard limit worth knowing: they are resolved against the module's
package, not the file system. `from ..x import y` in a top-level module raises
`ImportError: attempted relative import beyond top-level package`, which is Python telling you
there is no parent package, not that the path is wrong.

### `__init__.py` as the public API

`__init__.py` runs when the package is first imported. Anything it defines or imports becomes an
attribute of the package, which is what makes this work:

```python
from portfolio_ai import ConfigError   # even though ConfigError lives in exceptions.py
```

That convenience has a cost: **every import in `__init__.py` runs on every import of the
package**, including `import portfolio_ai.some.unrelated.thing`. Pull something heavy in and
every entry point pays for it. Pull in something that imports back, and you have a cycle.

So the rule here is narrow: `__init__.py` re-exports only from modules with no dependencies of
their own. Right now that is exactly one module, `exceptions.py`, which imports nothing at all.

`__all__` is a list of names that controls two things:

- what `from portfolio_ai import *` imports — the only thing most people know about it
- what documentation tools and linters treat as the public surface, which matters more

It does **not** restrict access. `portfolio_ai._private` is still reachable if someone types it.
Python's privacy is a naming convention and a linter rule, not a language feature.

### Circular imports

Two modules importing each other produces an error whose wording is genuinely unhelpful the
first time you meet it:

```
ImportError: cannot import name 'B' from partially initialized module 'b'
(most likely due to a circular import)
```

"Partially initialized" is the clue. Importing a module executes it top to bottom. If `a` imports
`b` at line 1, and `b` imports `a` at its own line 1, then `b` is asking for an `a` that has only
executed one line and does not have its names yet.

Three ways out, in order of preference:

1. **Extract the shared thing** into a third module both can import. Usually the cycle is telling
   you a genuine design problem — two modules that each know too much about the other.
2. **Move the import inside the function** that needs it. It then runs at call time, by which
   point both modules are fully loaded. Cheap, and slightly hides the dependency.
3. **`if TYPE_CHECKING:`** — for imports needed only for type annotations. The block never runs
   at runtime but type checkers still read it. Lesson 9 covers this.

The second half of your exercise is to produce this error deliberately, so that the next time you
see it in a real codebase you recognise the shape immediately.

### `py.typed`

An empty file with a large effect. By default, a type checker analysing *your* code will ignore
type hints inside an installed third-party package — it assumes the package is untyped and treats
everything it exports as `Any`.

PEP 561 says: ship a file called `py.typed` inside the package and checkers will trust your
annotations. Without it, all the typing work in lesson 9 would apply only inside this repository
and evaporate the moment the package was installed elsewhere.

### Locating files that are not code

`assistant/prompts/*.md` will hold the long, tuned prompts ported from n8n. They ship inside the
package, so how does the code find them at runtime?

The obvious approach is wrong:

```python
path = Path(__file__).parent / "prompts" / "rag_agent.md"   # fragile
```

It works from a normal directory and breaks when the package is inside a zip, a frozen
executable, or any loader that does not put real files on disk. The supported way:

```python
from importlib.resources import files

text = (files("portfolio_ai.assistant") / "prompts" / "rag_agent.md").read_text(encoding="utf-8")
```

`files()` asks the import system where the package's data actually is, whatever form it took.
Mentioned now because it is a packaging concern; used when the prompts arrive.

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

1. `sys.path[0]` is the current working directory, and it sits ahead of both the standard library
   and `site-packages`. Import takes the first match, so your file wins.

2. Python 3 imports are absolute by default (PEP 328). A bare `import logging` searches
   `sys.path`, where it finds the standard library; it does not look at sibling modules. Reaching
   our module requires saying so explicitly, with `from portfolio_ai import logging` or
   `from . import logging`. Python 2 behaved the other way, which is where the instinct comes from.

3. `__init__.py` runs on *every* import of the package, including
   `import portfolio_ai.something.unrelated`. A heavy import there is paid by every entry point,
   CLI startup included — and if the imported module imports back, you get a circular import.

4. It controls what `from package import *` brings in, and what documentation tools and linters
   treat as the public API. It does not restrict access: anything not in `__all__` is still
   importable by name. It is documentation, not enforcement.

5. It guarantees that every local run exercises the installed package rather than the source
   directory. A missing `__init__.py`, a file left out of the wheel or undeclared package data
   fails immediately on your machine, instead of at container start. The friction is the feature:
   it moves packaging errors from deploy time to development time.

6. Because type checkers ignore annotations in installed third-party packages by default,
   treating them as `Any`. `py.typed` (PEP 561) is the opt-in that says "these annotations are
   real, please use them". Without it, the strict typing from lesson 9 would stop mattering the
   moment the package was installed anywhere else.

</details>
