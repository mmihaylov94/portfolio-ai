"""What an answer scored before anyone's opinion of it: retrieval, and the rules.

Everything here is deterministic -- the same answer always gets the same result, and
nothing is spent. That makes it the part of an eval to trust first: when the judge
and a rule disagree, the rule is the one that can be read and checked.

**Retrieval is scored on documents, not chunks.** A question is answered by a
document -- "does Mihail know Python?" by ``tech-stack`` -- and which of its sections
came back first is a detail. So the chunks the model was shown are reduced to their
documents, in the order the model first met each one, and the case's expected
documents are looked for in that list:

- **recall**: the share of the expected documents that were found;
- **precision**: the share of the documents shown that were expected, which is what
  a smaller ``top_k`` should raise;
- **MRR**: one over the position of the first expected document -- 1.0 when it came
  first, 0.5 when second;
- **a hit**: any expected document found at all, which is when an answer is possible.

**The rules** are the answer-style and safety requirements in ``rag_agent.md`` that
can be checked with a pattern. A few are knowingly heuristics, and say so.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin

from portfolio_ai.assistant.postprocess import strip_forbidden_links, urls_in
from portfolio_ai.assistant.prompts.loader import RAG_AGENT, SMALL_TALK
from portfolio_ai.db.documents import RetrievedChunk
from portfolio_ai.evals.datasets import EvalCase

# The site the chat runs on. A relative link in an answer resolves against it.
SITE = "https://mihaylov.io/"

# rag_agent.md asks for two to four sentences by default. Six leaves room for an
# answer that offers more detail, and still catches the CV dump.
MAX_SENTENCES = 6

# Rule names -> what the rule found. Stored as JSON (eval_results.rule_violations),
# so the details are plain values: a count, a word, a list of strings.
type Violations = dict[str, object]


@dataclass(frozen=True)
class RetrievalScores:
    recall: float
    precision: float
    mrr: float

    @property
    def hit(self) -> bool:
        return self.recall > 0


def ranked_documents(chunks: Sequence[RetrievedChunk]) -> list[str]:
    """The documents behind ``chunks``, in the order the model first saw each one.

    A dict keeps the order its keys were inserted in, and ``setdefault`` inserts
    each document only the first time -- which makes this an ordered set.
    """
    seen: dict[str, None] = {}
    for chunk in chunks:
        seen.setdefault(chunk.doc_id, None)
    return list(seen)


def retrieval_scores(expected: Collection[str], shown: Sequence[str]) -> RetrievalScores | None:
    """Score the documents shown against the ones expected. None if none are expected."""
    if not expected:
        return None

    wanted = set(expected)
    found = [doc for doc in shown if doc in wanted]
    # enumerate(..., start=1) counts positions from one, which is what a rank is.
    first = next((rank for rank, doc in enumerate(shown, start=1) if doc in wanted), None)

    return RetrievalScores(
        recall=len(found) / len(wanted),
        precision=len(found) / len(shown) if shown else 0.0,
        mrr=1 / first if first else 0.0,
    )


# What can surround a link without being part of it: Markdown emphasis and angle
# brackets around a bare URL, and the punctuation that ends its sentence.
_WRAPPING = "*_<>"
_TRAILING = ".,;:!?"
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


def normalise_url(url: str) -> str:
    """A link reduced to where it goes, so one page compares equal however it is written.

    ``https://www.linkedin.com/in/x``, ``www.linkedin.com/in/x`` and
    ``**https://www.linkedin.com/in/x**.`` are the same page. So the wrapping comes
    off, a relative link is resolved against the site, and then the scheme, a leading
    ``www.`` and a trailing slash go too.
    """
    link = url.strip().strip(_WRAPPING)
    if link.startswith("/"):
        link = urljoin(SITE, link)
    link = link.rstrip(_TRAILING + _WRAPPING).rstrip("/").lower()
    return _SCHEME.sub("", link).removeprefix("www.")


# Every link the prompts themselves offer. A link in an answer is legitimate if it
# is one of these, or appears in what the model was shown; anything else it made up.
_OFFERED_BY_PROMPTS = frozenset(
    normalise_url(url) for prompt in (RAG_AGENT, SMALL_TALK) for url in urls_in(prompt.text)
)

# Two list lines make a list. One line starting with a dash is just punctuation.
MIN_BULLETS = 2

_SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
_HEADER = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*\*[^*\n]+\*\*:?\s*$)", re.MULTILINE)
# \N{BULLET} is the character by name, which the re module understands too: readable,
# and unlike the character itself it cannot be mistaken for a hyphen or a dot.
_BULLET = re.compile(r"^\s*(?:[-*\N{BULLET}]|\d+[.)])\s+\S", re.MULTILINE)
_GREETING = re.compile(r"^\s*(?:hi|hello|hey)\b", re.IGNORECASE)
_SELF_INTRODUCTION = re.compile(r"\b(?:i'm|i am|my name is|this is) rachel\b", re.IGNORECASE)
# Heuristic, and labelled so in reports: the phrasing of someone describing their
# own career. Rachel has no career, so from her these mean she is speaking as Mihail.
_AS_MIHAIL = re.compile(
    r"\bI(?:'ve| have)? (?:worked|built|developed|studied|graduated|founded|created|led"
    r"|designed|delivered|managed)\b"
    r"|\bmy (?:projects?|experience|career|cv|resume|portfolio|degrees?|clients?|employer)\b"
    # (?!') because \b alone would also match the possessive: an apostrophe is not a
    # word character, so "I'm Mihail\b" matches inside "I'm Mihail's assistant" --
    # the right answer to "Are you Mihail?". Apostrophes are straightened first.
    r"|\bI(?:'m| am) (?:a )?(?:solutions architect|mihail)\b(?!')",
    re.IGNORECASE,
)


def _plain(text: str) -> str:
    """Curly apostrophes straightened, as ``postprocess.is_fallback`` does."""
    return text.replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")


def _invented_links(answer: str, chunks: Sequence[RetrievedChunk]) -> list[str]:
    allowed = set(_OFFERED_BY_PROMPTS)
    for chunk in chunks:
        if chunk.url:
            allowed.add(normalise_url(chunk.url))
        allowed.update(normalise_url(url) for url in urls_in(chunk.content))
    return [
        url
        for url in urls_in(answer)
        # An email address or phone number written as a link is a contact detail, not
        # a page, and whether it is the right one is the judge's faithfulness to check.
        if not url.lower().startswith(("mailto:", "tel:")) and normalise_url(url) not in allowed
    ]


def rule_violations(
    case: EvalCase,
    *,
    route: str,
    answer: str,
    links_removed: int,
    chunks: Sequence[RetrievedChunk],
) -> Violations:
    """Every rule this answer breaks. An empty dict is a clean answer."""
    found: Violations = {}
    text = _plain(answer)
    lowered = text.lower()

    if route not in case.accepted_routes:
        found["misrouted"] = route

    # The filter caught a link the prompt forbids. The visitor never saw it, but the
    # model tried -- which means the prompt alone is not holding the rule.
    if links_removed:
        found["forbidden_link_attempted"] = links_removed
    _, still_there = strip_forbidden_links(answer)
    if still_there:
        found["forbidden_link_in_answer"] = still_there

    invented = _invented_links(answer, chunks)
    if invented:
        found["invented_link"] = invented

    missing = [phrase for phrase in case.must_include if _plain(phrase).lower() not in lowered]
    if missing:
        found["missing"] = missing
    present = [phrase for phrase in case.must_not_include if _plain(phrase).lower() in lowered]
    if present:
        found["forbidden_phrase"] = present

    if _HEADER.search(text):
        found["section_header"] = True
    bullets = len(_BULLET.findall(text))
    if bullets >= MIN_BULLETS:
        found["bullet_list"] = bullets
    sentences = len(_SENTENCE_END.findall(text.strip()))
    if sentences > MAX_SENTENCES:
        found["too_long"] = sentences

    if case.category != "small_talk" and _GREETING.match(text):
        found["unprompted_greeting"] = True
    # A case that asks who she is says so by requiring "Rachel" in the answer.
    asked_who = any(phrase.lower() == "rachel" for phrase in case.must_include)
    if not asked_who and _SELF_INTRODUCTION.search(text):
        found["introduced_self"] = True

    as_mihail = [match.group(0) for match in _AS_MIHAIL.finditer(text)]
    if as_mihail:
        found["spoke_as_mihail"] = as_mihail

    return found


def fallback_violations(
    case: EvalCase, *, route: str, fallback_used: bool, declined: bool | None
) -> Violations:
    """Whether the answer said "I don't know" exactly when it should have.

    ``declined`` is the judge's reading, which recognises any wording. Without a
    judge, the phrase match behind ``fallback_used`` stands in for it -- and the
    difference between the two, over a run, is how often that phrase match is wrong.
    """
    said_so = declined if declined is not None else fallback_used

    if case.expect_fallback and not said_so:
        return {"did_not_decline": True}
    if not case.expect_fallback and route == "mihail_related" and case.expected_doc_ids and said_so:
        return {"declined_answerable": True}
    return {}
