"""What happens to an answer after the model writes it and before a visitor reads it.

Two things:

- **The link rule, enforced in code.** Rachel must never send a visitor to a URL
  with a ``/knowledgebase`` or ``/projects`` path. The prompt says so twice, and it
  still is not a guarantee -- a prompt is a strong suggestion to a model, not a
  constraint on it. This is the constraint.
- **Recognising the fallback answer**, "I don't have that information...", which
  becomes ``fallback_used`` on the stored message: the most direct list there is of
  questions the knowledge base should answer and does not.

The link rule is the interesting part, because answers are streamed. A URL arrives
in pieces -- ``https://mihay``, ``lio.io/proj``, ``ects/threadline`` -- and nothing
can be sent until it is known whether the piece belongs to a forbidden link.
:class:`LinkFilter` holds back only as much as that question needs: the word in
progress, or a Markdown link that has been opened and not yet closed, together with
anything written straight against it -- and, rarely, the rest of a line holding a
"[" that a removal could still turn into a link. Everything before that goes out
immediately.
"""

import re
from urllib.parse import urlsplit

# A path segment named "knowledgebase" or "projects": "/projects/x", but also
# "/projects" at the end or before "?" or "#", which a plain substring test for
# "/projects/" let through. The site's own "#projects" section is a fragment, not a
# path segment, so it stays allowed. Case is ignored, so /Projects/ is caught too.
_FORBIDDEN_PATH = re.compile(r"/(knowledgebase|projects)(?=[/?#]|$)", re.IGNORECASE)

# Either a Markdown link or a bare URL, in one pattern, matched in one pass.
#
# One pass matters. Removing Markdown links first and bare URLs second would let the
# first pass create text the second then matches across -- and the result would
# depend on where the stream happened to be split, which is exactly the property
# the streaming filter below relies on not being true.
#
# The length limits are part of the same guarantee: the filter holds an unclosed
# "[" back for at most as long as the pattern could still match it.
#
# The portfolio's chat UI renders answers with a port of this pattern
# (app/utils/chat/answerText.ts in mmihaylov94/my-portfolio), so that nothing becomes
# a link there that this filter never looked at. Its tests check the port against a
# fixture of this pattern's own matches: change the two together, and regenerate the
# fixture with that repository's tests/unit/fixtures/link-parity.py.
_LINK = re.compile(
    # [label](target), or with a title: [label](target "title")
    r"\[(?P<label>[^\]\n]{0,300})\]\((?P<target>[^)\s]{0,2000})"
    r"(?P<title>[ \t]+(?:\"[^\"\n]{0,300}\"|'[^'\n]{0,300}'))?\)"
    # or https://..., http://... or www...., optionally in <angle brackets>
    r"|(?P<url><?(?:https?://|www\.)[^\s<>()\[\]\"'`]+>?)",
    re.IGNORECASE,
)

# The start of a Markdown link that has not finished arriving: "[label", "[label]",
# "[label](targ", '[label](target "tit'. If the unsent text from some "[" onwards
# looks like this, it has to wait -- the next piece might complete it into a link
# that must be removed.
_OPEN_LINK = re.compile(
    r"\[[^\]\n]{0,300}"
    r"(?:\](?:\([^)\s]{0,2000}(?:[ \t]+(?:\"[^\"\n]{0,300}\"?|'[^'\n]{0,300}'?)?)?)?)?"
)

# Sentence punctuation that follows a URL rather than belonging to it. Replacing
# "https://.../projects/x." should leave the full stop behind.
_TRAILING_PUNCTUATION = ".,;:!?"

# Markdown emphasis closing around a URL: the "**" in "**https://.../projects/x**".
# The URL pattern cannot tell it from the URL, because both characters are legal in
# one, so it comes off the end with the punctuation and goes back after the stand-in.
_EMPHASIS = "*_"

# What a forbidden bare URL on the site becomes: the nearest page that exists. Both
# are on rag_agent.md's list of links Rachel may give, so the stand-in is a link she
# could have written herself, and a unit test fails if either leaves that list.
PROJECTS_PAGE = "https://mihaylov.io/#projects"
HOME_PAGE = "https://mihaylov.io/"
_SITE_HOSTS = frozenset({"mihaylov.io", "www.mihaylov.io"})

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
    return _FORBIDDEN_PATH.search(url) is not None


def _stand_in(url: str) -> str | None:
    """The page on the site to send a visitor to instead of ``url``, if it has one.

    None for another site's URL -- a GitHub project board, say. Pointing a sentence
    that still names GitHub at the site would be a wrong link, so that URL is simply
    dropped. ``urlsplit`` finds a host only after "//", which "www.mihaylov.io/x"
    lacks, hence the scheme added in front; ``hostname`` comes back lower-cased.
    """
    try:
        host = urlsplit(url if "://" in url else f"https://{url}").hostname
    except ValueError:  # a malformed URL: treat it as nobody's
        return None
    if host not in _SITE_HOSTS:
        return None
    segments = {segment.lower() for segment in _FORBIDDEN_PATH.findall(url)}
    return PROJECTS_PAGE if "projects" in segments else HOME_PAGE


def strip_forbidden_links(text: str) -> tuple[str, int]:
    """Take every forbidden link out of ``text``. Returns the text and how many went.

    A bare forbidden URL on the site is replaced by the nearest real page: the
    projects section for a ``/projects`` URL, the home page for anything else. Only
    the URL changes; angle brackets and Markdown emphasis around it, and the
    punctuation after it, stay where they were. Deleting it instead left "read about
    it at **." behind, and deleting the words that led up to it is not an option: in
    a stream, they have already gone out by the time the URL arrives. Another site's
    forbidden URL has no page of ours to stand in for it, so it is deleted.

    A Markdown link to a forbidden URL keeps its label and loses the link, so "see
    [the case study](https://mihaylov.io/projects/x)" becomes "see the case study":
    pointing that label at the projects section would promise a page it is not.
    Allowed links are left exactly as written.

    The result is checked again, and again, until a check finds nothing, because a
    removal can join the text around it into a new link. One pass of ``re.sub`` never
    looks at its own replacements: "[https://mihaylov.io](https://.../projects/x)" keeps
    its label, and a "/projects/x" written straight after it, with no space, joins the
    label into "https://mihaylov.io/projects/x". So does a URL written straight before
    a link whose kept label starts "/projects". The second pass catches what the
    first one made.

    The loop always ends. Every pass that changes the text takes out at least one
    character the answer was written with -- a stand-in on its own is never a
    forbidden URL, so every forbidden link holds some -- and no pass adds any. In
    practice one pass finds everything, and a second finds nothing.

    Three gaps are known and accepted, because none comes from an ordinary answer:

    - A reference-style definition, ``[1]: https://...``, is not read as a Markdown
      link, so its URL is treated as a bare one.
    - A URL without "https://" or "www.", such as ``mihaylov.io/projects/x``, is not
      recognised as a link at all. The chat UI renders answers with this module's own
      link pattern, so it never links one either; a renderer that turned bare domains
      into links (markdown-it's ``linkify``) would make this a live one.
    - A path written percent-encoded, such as ``https://mihaylov.io/%70rojects/x``,
      is not seen as "/projects", though the web server decodes it into one. It
      leads where "/projects/x" would: to a page that does not exist.
    """
    total = 0
    while True:
        text, removed = _strip_once(text)
        if removed == 0:
            return text, total
        total += removed


def _strip_once(text: str) -> tuple[str, int]:
    """One pass of :func:`strip_forbidden_links`, which may leave new links behind."""
    removed = 0

    # Called once per match by re.sub, and returns what the match is replaced with.
    def replace(match: re.Match[str]) -> str:
        # `nonlocal` lets this inner function reassign `removed` in the enclosing
        # one. Without it, `removed += 1` would create a new local variable here and
        # fail, because it is read before it is ever assigned.
        nonlocal removed

        url = match.group("url")
        if url is not None:
            # The URL itself, without what can close around it in any order:
            # "x**." and "x.**" both come down to "x".
            bare = url.strip("<>")
            link = bare.rstrip(_TRAILING_PUNCTUATION + _EMPHASIS)
            if not is_forbidden_url(link):
                return url
            removed += 1
            stand_in = _stand_in(link)
            if stand_in is None:
                # Only the punctuation after it stays, as the filter always did.
                return "".join(c for c in bare[len(link) :] if c in _TRAILING_PUNCTUATION)
            # The link is where the match starts, after an optional "<", so this
            # swaps it and keeps everything around it.
            return url.replace(link, stand_in, 1)

        # A label can itself be a URL -- "[https://.../projects/x](...)" -- and it
        # is shown to the visitor, so it gets the same treatment.
        label, removed_from_label = strip_forbidden_links(match.group("label"))
        removed += removed_from_label

        if is_forbidden_url(match.group("target")):
            removed += 1
            return label
        return f"[{label}]({match.group('target')}{match.group('title') or ''})"

    return _LINK.sub(replace, text), removed


def urls_in(text: str) -> list[str]:
    """Every link in ``text``: bare URLs, and the targets of Markdown links.

    The same pattern the filter matches, so what counts as a link here is exactly
    what the filter looks at. Angle brackets, trailing sentence punctuation and
    closing emphasis come off, as they do when the filter replaces a link. The evals
    use this to catch a link the model made up, which the filter has no reason to
    touch.
    """
    found: list[str] = []
    for match in _LINK.finditer(text):
        url = match.group("url")
        if url is not None:
            found.append(url.strip("<>").rstrip(_TRAILING_PUNCTUATION + _EMPHASIS))
        else:
            found += urls_in(match.group("label"))
            found.append(match.group("target"))
    return found


# Where the scan below has something to decide: a "[", which may start a link, or
# whitespace, which may be a place to cut.
_STOP = re.compile(r"[\[\s]")


def _settled(link: re.Match[str]) -> bool:
    """Whether a finished Markdown link can have the text after it sent on its own.

    A link that stays (its target is allowed) stays exactly where it is, on every
    pass of :func:`strip_forbidden_links`. A link that goes leaves its label behind,
    and a label holding a "[" can open a new link with what follows it:
    "[[](...)a b](...)" leaves "[a b](...)".
    """
    return "[" not in link.group("label") or not is_forbidden_url(link.group("target"))


class LinkFilter:
    """Takes forbidden links out of text that arrives in pieces.

    Feed it each piece; it returns what is safe to send now. Call :meth:`finish`
    when the stream ends, for whatever it was still holding. Everything returned,
    concatenated, is exactly what :func:`strip_forbidden_links` would return for the
    whole text at once -- the unit tests check that for every way of splitting it.

    **Where it cuts.** Only just after whitespace, because a bare URL never contains
    any, and the word in progress may still turn out to be one. A link that spans a
    space can only be a Markdown link with spaces in its label, and no link reaches
    past the end of a line. So a space is safe to cut at when every "[" before it on
    its line starts a finished link that is settled (see :func:`_settled`).

    Any other "[" holds back what follows it. A link still arriving waits until it
    is finished. Every other kind holds the rest of its line, and that includes a "["
    that is not a link as written, because a removal can make it one: in
    "[a b]https://github.com/u/projects/1(...)", the deleted URL leaves "[a b](...)".
    What is written straight against a link, such as "https://mihaylov.io" in front
    of "[/projects](...)", has no space between and waits with it.

    Nothing can join text across a cut like that, on any pass: a link that stays
    keeps its place, and the label of a link that goes has no "[" to start a new one.
    So cleaning what goes out piece by piece gives exactly what cleaning the whole
    text would.

    **What it costs.** It looks at each character once, and keeps its place between
    pieces, because it runs on the event loop for every piece of every answer. Only
    a link still arriving is looked at again with each new piece, and that costs no
    more than the longest link the pattern allows. An earlier version looked at the
    whole held text again for every piece, and crafted text took it minutes.
    """

    def __init__(self) -> None:
        self._pending = ""  # received and not yet sent
        self._scanned = 0  # how far into it the scan has looked
        self._cut = 0  # how much of it can be sent so far
        self._line_held = False  # the line being scanned has to end before any more goes
        self.removed = 0

    def feed(self, text: str) -> str:
        self._pending += text
        self._scan()
        cut = self._cut
        self._scanned -= cut
        self._cut = 0
        return self._send(cut)

    def finish(self) -> str:
        self._scanned = 0
        self._cut = 0
        self._line_held = False
        return self._send(len(self._pending))

    def _scan(self) -> None:
        """Move the cut as far through the held text as is safe, from where it got to."""
        text = self._pending
        i = self._scanned
        while i < len(text):
            if self._line_held:
                end = text.find("\n", i)
                if end == -1:
                    i = len(text)
                    break
                # No link reaches past the end of a line: everything up to it is settled.
                self._line_held = False
                i = end + 1
                self._cut = i
                continue

            stop = _STOP.search(text, i)
            if stop is None:
                i = len(text)
                break
            i = stop.start()
            if text[i] != "[":
                # Whitespace that no link can span.
                i += 1
                self._cut = i
                continue

            link = _LINK.match(text, i)
            if link is not None:
                if _settled(link):
                    i = link.end()  # past its label, whose spaces are no place to cut
                else:
                    self._line_held = True
            elif _OPEN_LINK.fullmatch(text, i):
                break  # still arriving: looked at again when the next piece comes
            else:
                self._line_held = True
        self._scanned = i

    def _send(self, cut: int) -> str:
        ready, self._pending = self._pending[:cut], self._pending[cut:]
        if not ready:
            return ""
        cleaned, removed = strip_forbidden_links(ready)
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
