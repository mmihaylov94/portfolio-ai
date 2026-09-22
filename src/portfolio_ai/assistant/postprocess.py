"""What happens to an answer after the model writes it and before a visitor reads it.

Two things:

- **The link rule, enforced in code.** Rachel must never send a visitor to a URL
  containing ``/knowledgebase/`` or ``/projects/``. The prompt says so twice, and it
  still is not a guarantee -- a prompt is a strong suggestion to a model, not a
  constraint on it. This is the constraint.
- **Recognising the fallback answer**, "I don't have that information...", which
  becomes ``fallback_used`` on the stored message: the most direct list there is of
  questions the knowledge base should answer and does not.

The link rule is the interesting part, because answers are streamed. A URL arrives
in pieces -- ``https://mihay``, ``lio.io/proj``, ``ects/threadline`` -- and nothing
can be sent until it is known whether the piece belongs to a forbidden link.
:class:`LinkFilter` holds back only as much as that question needs: the word in
progress, or a Markdown link that has been opened and not yet closed. Everything
before that goes out immediately.
"""

import re

# Lower-case; URLs are compared lower-cased, so /Projects/ is caught too.
FORBIDDEN_SEGMENTS = ("/knowledgebase/", "/projects/")

# Either a Markdown link or a bare URL, in one pattern, matched in one pass.
#
# One pass matters. Removing Markdown links first and bare URLs second would let the
# first pass create text the second then matches across -- and the result would
# depend on where the stream happened to be split, which is exactly the property
# the streaming filter below relies on not being true.
#
# The length limits are part of the same guarantee: the filter holds an unclosed
# "[" back for at most as long as the pattern could still match it.
_LINK = re.compile(
    # [label](target)
    r"\[(?P<label>[^\]\n]{0,300})\]\((?P<target>[^)\s]{0,2000})\)"
    # or https://..., http://... or www...., optionally in <angle brackets>
    r"|(?P<url><?(?:https?://|www\.)[^\s<>()\[\]\"'`]+>?)",
    re.IGNORECASE,
)

# The start of a Markdown link that has not finished arriving: "[label", "[label]",
# "[label](targ". If the unsent text from some "[" onwards looks like this, it has to
# wait -- the next piece might complete it into a link that must be removed.
_OPEN_LINK = re.compile(r"\[[^\]\n]{0,300}(?:\](?:\([^)\s]{0,2000})?)?")

# Sentence punctuation that follows a URL rather than belonging to it. Removing
# "https://.../projects/x." should leave the full stop behind.
_TRAILING_PUNCTUATION = ".,;:!?"

_WHITESPACE = re.compile(r"\s")

# The fallback wording rag_agent.md dictates -- rule 2's sentence, and rule 7's
# "do not have enough information" -- in the forms the model produces them.
# Lower-case with straight apostrophes; see is_fallback. A unit test checks the
# prompt's own sentence still matches, so the two cannot drift apart unnoticed.
_FALLBACK_PHRASES = (
    "don't have that information",
    "do not have that information",
    "don't have enough information",
    "do not have enough information",
)


def is_forbidden_url(url: str) -> bool:
    lowered = url.lower()
    return any(segment in lowered for segment in FORBIDDEN_SEGMENTS)


def strip_forbidden_links(text: str) -> tuple[str, int]:
    """Remove every forbidden link from ``text``. Returns the text and how many went.

    A bare forbidden URL is removed, leaving any punctuation that followed it. A
    Markdown link to a forbidden URL keeps its label and loses the link, so
    "see [the case study](https://mihaylov.io/projects/x)" becomes "see the case
    study" rather than "see ". Allowed links are left exactly as written.
    """
    removed = 0

    # Called once per match by re.sub, and returns what the match is replaced with.
    def replace(match: re.Match[str]) -> str:
        # `nonlocal` lets this inner function reassign `removed` in the enclosing
        # one. Without it, `removed += 1` would create a new local variable here and
        # fail, because it is read before it is ever assigned.
        nonlocal removed

        url = match.group("url")
        if url is not None:
            bare = url.strip("<>")
            core = bare.rstrip(_TRAILING_PUNCTUATION)
            if not is_forbidden_url(core):
                return url
            removed += 1
            return bare[len(core) :]

        # A label can itself be a URL -- "[https://.../projects/x](...)" -- and it
        # is shown to the visitor, so it gets the same treatment.
        label, removed_from_label = strip_forbidden_links(match.group("label"))
        removed += removed_from_label

        if is_forbidden_url(match.group("target")):
            removed += 1
            return label
        return f"[{label}]({match.group('target')})"

    return _LINK.sub(replace, text), removed


def _safe_cut(text: str) -> int:
    """How much of ``text`` can be cleaned now without knowing what comes next.

    Two rules. A bare URL never contains whitespace, so text up to the last
    whitespace cannot be part of a URL still arriving. And a Markdown link can
    contain spaces in its label, so text is never cut inside one -- finished or not.
    """
    cut = 0
    for match in _WHITESPACE.finditer(text):
        cut = match.end()

    position = 0
    while (start := text.find("[", position, cut)) != -1:
        link = _LINK.match(text, start)
        if link is not None and link.group("url") is None:
            if link.end() > cut:
                return start  # a finished link that the cut would split
            position = link.end()
        elif _OPEN_LINK.fullmatch(text, start):
            return start  # a link still arriving
        else:
            position = start + 1

    return cut


class LinkFilter:
    """Removes forbidden links from text that arrives in pieces.

    Feed it each piece; it returns what is safe to send now. Call :meth:`finish`
    when the stream ends, for whatever it was still holding. Everything returned,
    concatenated, is exactly what :func:`strip_forbidden_links` would return for the
    whole text at once -- the unit tests check that for every way of splitting it.
    """

    def __init__(self) -> None:
        self._pending = ""
        self.removed = 0

    def feed(self, text: str) -> str:
        self._pending += text
        cut = _safe_cut(self._pending)
        ready, self._pending = self._pending[:cut], self._pending[cut:]
        return self._clean(ready)

    def finish(self) -> str:
        ready, self._pending = self._pending, ""
        return self._clean(ready)

    def _clean(self, text: str) -> str:
        if not text:
            return ""
        cleaned, removed = strip_forbidden_links(text)
        self.removed += removed
        return cleaned


def is_fallback(reply: str) -> bool:
    """Whether ``reply`` is the "I don't have that information" answer.

    A phrase match, and knowingly a heuristic: it recognises the wording the prompt
    tells the model to use, and a model that paraphrases it will be missed. Curly
    apostrophes (U+2019) are straightened first -- the prompt itself spells "don't"
    with one, and the model copies whichever it saw. The evals measure how often
    this is wrong.
    """
    # Written as an escape rather than the character itself, which is
    # indistinguishable from a straight apostrophe in most editors.
    normalised = reply.lower().replace("\u2019", "'")
    return any(phrase in normalised for phrase in _FALLBACK_PHRASES)
