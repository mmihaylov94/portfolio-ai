"""The fixed rules a contact form message meets first: clean, validate, score for spam.

No model and no network. A message that fails here never costs a model call: an
invalid one, or one whose score reaches :data:`SPAM_SCORE`, is reported to the owner
as blocked and goes no further.

**This is a port, and it is held to its original.** The rules were a Code node in
the n8n workflow this replaces, written in JavaScript. They are reproduced here as
they were, the odd ones included (the list is in docs/CONTACT.md), because a rule
changed in passing is a change nobody measured. ``tests/unit/test_contact_rules.py``
runs a fixture made by n8n's own code under Node, and this module has to give the
same verdict for every submission in it.

Staying faithful to JavaScript takes care in four places, each marked below where
it bites:

- **What counts as white space.** JavaScript's ``\\s`` and Python's are different
  lists, so the class is spelt out (:data:`_SPACE`).
- **How long a string is.** JavaScript counts UTF-16 code units, so an emoji is 2.
  Python counts characters, so it is 1. ``message.length < 10`` is a rule.
- **What a word is.** ``\\b`` and ``/i`` are ASCII-only in a JavaScript pattern
  without the ``u`` flag, and Unicode-wide in Python unless ``re.ASCII`` says not.
- **Where a string ends.** ``$`` in Python also matches before a final line break;
  ``fullmatch`` does not.
"""

import re
from dataclasses import dataclass
from typing import Final, Literal, get_args

type Reason = Literal["project", "collaboration", "job", "consultation", "other"]

# The same five the site's form offers, read off the type so that there is one list.
# `Reason.__value__` is what a `type` statement's alias holds: the Literal itself.
REASONS: Final[frozenset[str]] = frozenset(get_args(Reason.__value__))

# A score of this or more is spam, and blocked without asking a model.
SPAM_SCORE = 5
# A score of this or more is suspicious: it goes on to the classifier, and it gets
# no automatic reply whatever the classifier says.
SUSPICIOUS_SCORE = 3

# JavaScript's \s, character for character (ECMAScript's WhiteSpace and
# LineTerminator). Python's own \s would add U+001C to U+001F and U+0085, and leave
# out U+FEFF.
#
# A raw string, like every pattern below: the backslashes are for the regular
# expression engine to read, which knows these escapes itself. Left to Python, each
# would become the invisible character it names before the engine ever saw it.
_SPACE = r"\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_SPACES = re.compile(rf"[{_SPACE}]+")
# Zero-width characters, which are invisible and so a way past a keyword match.
_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")
_TAG = re.compile(r"<[^>]*>")
_EMAIL = re.compile(rf"[^{_SPACE}@]+@[^{_SPACE}@]+\.[^{_SPACE}@]+")
# re.ASCII with re.IGNORECASE, on every pattern that ignores case: \b sees only ASCII
# letters as part of a word, and a letter matches its other case only when both are
# ASCII, which is what `/i` did in JavaScript. Without it a long s (U+017F) followed
# by "eo" would match "seo" here and not there, and "http" with a long s after it
# would be a link here and not there.
_FLAGS = re.IGNORECASE | re.ASCII
_URL = re.compile(rf"https?://[^{_SPACE}]+|www\.[^{_SPACE}]+", _FLAGS)
# Anything but a letter, a digit, white space and a little punctuation.
_ORDINARY = re.compile(rf"[a-zA-Z0-9{_SPACE}.,!?@:/-]")

# The numbers in the rules, named. Each is the first value that trips its rule, or
# the last that does not, as its comment says.
_SHORT_MESSAGE = 10  # a message shorter than this is "too short"
_SHORT_SUBJECT = 2  # and a subject shorter than this
_LINKS_ALLOWED = 2  # more links than this is "too many"
_SYMBOLS_ALLOWED = 10  # more characters that are not ordinary than this
_WORDS_TO_JUDGE = 6  # repetition is not looked for in fewer words
_OFTEN = 4  # a word used this many times is repeated
_REPEATED_WORDS = 2  # and this many such words is "excessive repetition"
_LETTERS_TO_JUDGE = 10  # capitals are not counted in fewer letters
_SHOUTED_SHARE = 0.6  # more than this share of capitals is "too much uppercase"
_MANY_KEYWORDS = 3  # this many spam keywords is a second hit

_DISPOSABLE_DOMAINS = frozenset(
    {
        "mailinator.com",
        "tempmail.com",
        "10minutemail.com",
        "guerrillamail.com",
        "sharklasers.com",
    }
)

_SPAM_PATTERNS = tuple(
    re.compile(pattern, _FLAGS)
    for pattern in (
        r"seo",
        r"backlinks?",
        r"guest post",
        r"link exchange",
        r"domain authority",
        r"\bda\b",
        r"\bdr\b",
        r"casino",
        r"betting",
        r"forex",
        r"crypto",
        r"telegram",
        r"whatsapp",
        r"sponsored post",
        r"increase traffic",
        r"boost traffic",
        r"buy now",
        r"100% guarantee",
    )
)
_NOT_A_JOB = re.compile(r"seo|guest post|backlink|traffic", _FLAGS)


@dataclass(frozen=True)
class Checked:
    """One submission after the rules: its fields cleaned, and what the rules found."""

    email: str
    subject: str
    message: str
    reason: str
    is_valid: bool
    is_spam_rule: bool
    suspicious: bool
    spam_score: int
    # In the order the rules ran. Fixed wording with a count in two of them, and
    # nothing a visitor typed, so these may be logged.
    rule_hits: tuple[str, ...]
    # Why it was blocked, or "" when it was not.
    block_reason: str

    @property
    def blocked(self) -> bool:
        return not self.is_valid or self.is_spam_rule


def _length(text: str) -> int:
    """How long JavaScript says a string is: in UTF-16 code units.

    A character outside the Basic Multilingual Plane, an emoji say, is two of them.
    Encoding to UTF-16 and halving the byte count is the short way to ask.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _clean(value: str) -> str:
    """Zero-width characters out, every run of white space to one space, ends trimmed."""
    return _SPACES.sub(" ", _ZERO_WIDTH.sub("", value)).strip(" ")


def _without_tags(value: str) -> str:
    return _TAG.sub(" ", value)


def _repeats_itself(text: str) -> bool:
    """Two or more different words used four times or more each, in six words or more."""
    words = [word for word in _SPACES.split(text.lower()) if word]
    if len(words) < _WORDS_TO_JUDGE:
        return False
    counts: dict[str, int] = {}
    for word in words:
        counts[word] = counts.get(word, 0) + 1
    return sum(count >= _OFTEN for count in counts.values()) >= _REPEATED_WORDS


def _symbols(text: str) -> int:
    """How many characters are not ordinary ones, counted as JavaScript counts."""
    return sum(_length(char) for char in text if not _ORDINARY.fullmatch(char))


def _shouts(text: str) -> bool:
    """More than six in ten of its ASCII letters are capitals, given at least ten."""
    letters = [char for char in text if char.isascii() and char.isalpha()]
    if len(letters) < _LETTERS_TO_JUDGE:
        return False
    return sum(char.isupper() for char in letters) / len(letters) > _SHOUTED_SHARE


def _refusal(email: str, subject: str, message: str, reason: str) -> str:
    """Why cleaned fields are not a valid submission, or "" when they are.

    In this order, and the first one found is the answer: something missing, then a
    reason the form does not offer, then an address that is not one.
    """
    if not (email and subject and message and reason):
        return "Missing required fields"
    if reason not in REASONS:
        return "Invalid reason value"
    if not _EMAIL.fullmatch(email):
        return "Invalid email format"
    return ""


def _score(email: str, subject: str, message: str, reason: str) -> tuple[int, tuple[str, ...]]:
    """The spam score of a valid submission, and the rules that made it up.

    One ``if`` to a rule, in the order the original ran them, which is the order the
    hits are listed in the owner's notification.
    """
    hits: list[tuple[int, str]] = []

    if email.partition("@")[2] in _DISPOSABLE_DOMAINS:
        hits.append((2, "Disposable email domain"))
    if _length(message) < _SHORT_MESSAGE:
        hits.append((2, "Message too short"))
    if _length(subject) < _SHORT_SUBJECT:
        hits.append((1, "Subject too short"))
    links = len(_URL.findall(message))
    if links > _LINKS_ALLOWED:
        hits.append((2, f"Too many links: {links}"))
    if _repeats_itself(message):
        hits.append((2, "Excessive repetition"))
    if _symbols(message) > _SYMBOLS_ALLOWED:
        hits.append((1, "Too many special characters"))
    if _shouts(message):
        hits.append((1, "Too much uppercase text"))
    if subject.lower() == message.lower():
        hits.append((1, "Subject and message are identical"))

    keywords = sum(
        bool(pattern.search(subject) or pattern.search(message)) for pattern in _SPAM_PATTERNS
    )
    if keywords:
        hits.append((2, f"Spam keyword hits: {keywords}"))
    if keywords >= _MANY_KEYWORDS:
        hits.append((1, "Multiple spam indicators"))
    if reason == "job" and _NOT_A_JOB.search(message):
        hits.append((1, "Reason/message mismatch"))

    return sum(points for points, _ in hits), tuple(what for _, what in hits)


def check(*, email: str, subject: str, message: str, reason: str) -> Checked:
    """Clean one submission and run the rules over it.

    Validity is decided on the cleaned fields, and the score is only worked out for
    a valid submission: an invalid one has a score of nought and no hits.
    """
    email = _clean(email).lower()
    subject = _clean(_without_tags(subject))
    message = _clean(_without_tags(message))
    reason = _clean(reason).lower()

    refusal = _refusal(email, subject, message, reason)
    if refusal:
        return Checked(
            email=email,
            subject=subject,
            message=message,
            reason=reason,
            is_valid=False,
            is_spam_rule=False,
            suspicious=False,
            spam_score=0,
            rule_hits=(),
            block_reason=refusal,
        )

    score, hits = _score(email, subject, message, reason)
    is_spam = score >= SPAM_SCORE
    return Checked(
        email=email,
        subject=subject,
        message=message,
        reason=reason,
        is_valid=True,
        is_spam_rule=is_spam,
        suspicious=not is_spam and score >= SUSPICIOUS_SCORE,
        spam_score=score,
        rule_hits=hits,
        block_reason="Rule-based spam threshold reached" if is_spam else "",
    )
