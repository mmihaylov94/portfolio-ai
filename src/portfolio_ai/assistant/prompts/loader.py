"""The prompts, read once from the Markdown files beside this module.

They are files rather than string literals because they are long, they are the main
thing anyone will tune, and a change to one should read as a diff of prose rather
than a diff of a Python string full of escaped quotes.

Each file opens with a small YAML block: a ``version``, where the text came from,
and what was changed on the way in. Five were ported from the n8n workflow this
project replaces, word for word apart from the two corrections listed in
``rag_agent.md``. One has since been changed on purpose, after an eval run: the
classifier, at version 2, adds to n8n's text without removing any of it. Two are
new: the reply sent once the daily spending limit is reached (n8n had no limit),
and the eval judge's rubric.

**The version is recorded with every answer** (``chat_messages.llm_calls``) and with
every eval run (``eval_runs.config``), so a score can always be traced to the prompt
that produced it. That only works if the version actually changes when the text does, so
``tests/unit/test_prompts.py`` pins each prompt's content hash to its version. Edit a
prompt without bumping its version and CI fails -- which is CLAUDE.md's "ask before
changing the tuned prompts", written as something a machine checks.

Loaded at import, deliberately. A missing or malformed prompt is a packaging bug,
and it should stop the process the moment the assistant is imported, rather than
surface as a 500 on the first visitor's first question.
"""

import hashlib
from dataclasses import dataclass
from importlib import resources

import yaml

from portfolio_ai.ingestion.frontmatter import split_frontmatter

# Read through importlib.resources rather than a path built from __file__. The
# package is installed into the image's virtualenv (the Dockerfile's --no-editable),
# and resources is the API that finds files inside an installed package wherever
# it ended up -- in a directory today, possibly inside a zip tomorrow.
_PACKAGE = "portfolio_ai.assistant.prompts"


@dataclass(frozen=True)
class Prompt:
    """One prompt: its text, and enough identity to trace an answer back to it."""

    name: str
    version: int
    text: str

    @property
    def ref(self) -> str:
        """``rag_agent@1`` -- what gets recorded alongside every call that used it."""
        return f"{self.name}@{self.version}"

    @property
    def digest(self) -> str:
        """A short hash of the text, which the version-pinning test compares against.

        Twelve hex characters is 48 bits. The question it answers is "did this text
        change", asked of a handful of files, so collisions are not a real consideration.
        """
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


def load(name: str) -> Prompt:
    """Read ``<name>.md`` from this package."""
    raw = resources.files(_PACKAGE).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return parse(name, raw)


def parse(name: str, raw: str) -> Prompt:
    """Turn a prompt file's contents into a :class:`Prompt`, checking its version.

    Separate from :func:`load` so the checks can be tested on a string, without
    planting broken files inside the package to do it.
    """
    block, body = split_frontmatter(raw)
    meta = yaml.safe_load(block) or {}

    version = meta.get("version")
    # bool is a subclass of int, so `version: true` would otherwise pass as 1.
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError(f"prompt {name!r} needs a positive integer version in its frontmatter")

    text = body.strip()
    if not text:
        raise ValueError(f"prompt {name!r} is empty")

    return Prompt(name=name, version=version, text=text)


CLASSIFIER = load("classifier")
SMALL_TALK = load("small_talk")
RAG_AGENT = load("rag_agent")
# The retrieval tool's description. Not a system prompt, but it is read by the model
# on every knowledge-base question and was tuned alongside the others -- the
# query-rewriting instructions live here as much as in rag_agent.md.
SEARCH_TOOL = load("search_tool")
# Not a prompt at all: the fixed reply to out-of-scope questions, which costs no
# model call. It lives here because it is tuned wording like the rest.
OUT_OF_SCOPE_REPLY = load("out_of_scope_reply")
# Also fixed wording: what the API says instead of answering once the day's spending
# limit is reached (DAILY_SPEND_CAP_USD). Visitors read it, so it is versioned too.
DAILY_LIMIT_REPLY = load("daily_limit_reply")
# Not Rachel's at all: the rubric the eval harness grades her answers with. Versioned
# like the rest, because a change to how answers are graded moves every score as
# surely as a change to how they are written.
JUDGE = load("judge")

ALL = (
    CLASSIFIER,
    SMALL_TALK,
    RAG_AGENT,
    SEARCH_TOOL,
    OUT_OF_SCOPE_REPLY,
    DAILY_LIMIT_REPLY,
    JUDGE,
)
