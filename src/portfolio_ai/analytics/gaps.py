"""Content gaps: what visitors asked for that the knowledge base does not have.

A gap is a group of failed questions that one article would close. :func:`refresh`
works the groups out again from everything still stored and brings the
``content_gaps`` table up to date; the report and the weekly email read the table.

**What counts as failed.** A knowledge-base answer that was a fallback, or whose best
passage scored under ``LOW_SCORE_THRESHOLD``. On the invented conversations that is
20 of the 26 questions written to be unanswerable and 3 of the 58 written to be
answerable (docs/ANALYTICS.md).

**What is clustered is the search, not the question.** Every knowledge-base answer
stored the query the model searched with: a few topical keywords, with the name
"Mihail" and the filler taken out, and with a follow-up's context put back, so that
"What about Go?" was searched as "Go programming language experience". Clustered as
typed, the questions grouped by their phrasing: everything that began "Does he
have" in one group, everything that said "Mihail" in another. Clustered by their
searches, the same questions fell into rates, salary, references, hobbies and the
rest. The rewrite was built to make retrieval work, and it is what makes this work.

**A gap is recognised across refreshes by its centre.** Each refresh clusters from
scratch, so the groups have no ids of their own. A new group whose centre is at
least :data:`SAME_GAP` like a stored gap's is that gap: its counts are replaced and
its label and status are kept, which is what lets a gap marked ``wont_fix`` stay
that way. A group like no stored gap is a new one, and is named by a model.

**A label is a model's output, and is treated as input.** It is written from what
visitors typed, it is kept for good, and it is printed in a terminal and in an
email. So it is checked like anything else that arrives from outside
(:func:`_fault`), and what the model is shown of each question is one clipped line
(:func:`brief`).

**The order of work protects the table.** Everything that can fail on its own is
done before anything is written: the embeddings, then the names. The writes then
happen in one transaction. A refresh that dies at OpenAI leaves the table as it was.
"""

import dataclasses
import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

import structlog
from openai import APIError
from pydantic import BaseModel

from portfolio_ai.analytics import clustering
from portfolio_ai.analytics.signals import cosine
from portfolio_ai.assistant.prompts.loader import GAP_LABEL
from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db.analytics import FailedQuestion, GapCounts
from portfolio_ai.llm import responses
from portfolio_ai.llm.embeddings import embed_texts
from portfolio_ai.llm.responses import domain_error

log = structlog.get_logger(__name__)

# How alike the centre of a new group and of a stored gap must be for them to be one
# gap. Far stricter than GAP_SIMILARITY, and a different question: that one asks
# whether a search belongs in a group, this one whether two groups are the same.
#
# Measured on the 24 failed questions in the development database (2026-10-13):
#
# - two different gaps' centres were at most 0.47 alike;
# - a gap that gained or lost one question, or was first seen as one question and
#   then as its whole group, stayed at least 0.86 like itself (seven groups of two
#   to four);
# - a group whose questions were all replaced by others of the same group could be
#   as little as 0.47 like itself (each group split into two parts sharing no
#   question, at its least alike split). No value tells that from a different gap,
#   and here it becomes a new one.
#
# The two mistakes do not cost the same, which is why this sits nearer the top of
# the space between. Calling two subjects one gap files a new subject under an old
# label and an old decision, and nobody is told. Calling one gap two makes a second
# row in the queue, which a person sees. The arithmetic edge of the second kind: a
# gap of one question that gains four, each exactly 0.45 like it and like each
# other, moves to 0.748 and is taken for a new gap.
#
# A constant and not a setting until there is a reason to move it.
SAME_GAP = 0.75

# How many of a gap's questions it keeps the ids of, as examples: the ones nearest
# its centre, which are the ones most typical of it.
EXAMPLES = 5

# How many failed questions one refresh groups: the newest. Grouping compares every
# search with every centre, so its time grows with the square of this: 500 searches
# with nothing in common took 11 seconds on the development machine, and at that
# rate 5,000 would take twenty minutes of a server that is also serving the site.
# The spending limit allows about 1,600 answers a day, so the number stored is not
# small by nature.
MAX_QUESTIONS = 500

# How many gaps one refresh asks a model to name. The rest are stored unnamed and
# are named by the refreshes after it, the most asked first. Naming is outside
# DAILY_SPEND_CAP_USD, and this is what bounds it: twenty calls are a third of a cent.
MAX_NAMED = 20

# How many of a group's questions the model is shown when it names the gap, and how
# much of each.
QUESTIONS_SHOWN = 12
SHOWN_LENGTH = 200

# What a gap is called when the model's answer could not be used, or was not asked
# for yet. Every refresh asks again for a gap still called this.
UNNAMED = "Unnamed gap"
MAX_LABEL = 60

# A label has no business holding a link or an address, and a model that was talked
# into writing one is the case this is for. "mihaylov.io AI assistant" is a project's
# name and passes: it has none of the three.
_LINK_OR_ADDRESS = re.compile(r"://|www\.|@", re.IGNORECASE)


# The shape the model must answer in. No docstring, for the reason the classifier's
# has none: it would become the schema's description, which the model reads.
class GapLabel(BaseModel):
    label: str


@dataclass(frozen=True)
class Group:
    """One cluster of failed questions, and what it means for the table."""

    questions: list[FailedQuestion]
    counts: GapCounts
    # The stored gap this is, or None for a gap seen for the first time.
    gap_id: int | None
    # The stored gap's label, or the one a model has just written, or None in a dry
    # run of a new gap, where nothing was asked.
    label: str | None


@dataclass(frozen=True)
class Refresh:
    """What one refresh found, and what it cost."""

    failed: int
    groups: list[Group]
    # Stored gaps that no group matched: nobody has asked lately. In a dry run, how
    # many the refresh it describes would empty.
    emptied: int
    embedded: int
    cost: Decimal
    dry_run: bool
    # More failed questions are stored than one refresh groups, and the oldest were
    # left out.
    capped: bool = False

    @property
    def created(self) -> list[Group]:
        return [group for group in self.groups if group.gap_id is None]

    @property
    def updated(self) -> list[Group]:
        return [group for group in self.groups if group.gap_id is not None]

    @property
    def unnamed(self) -> int:
        """How many gaps are still waiting for a name after this refresh."""
        return sum(group.label == UNNAMED for group in self.groups)


def counts_for(
    questions: list[FailedQuestion], centre: list[float], examples: list[int]
) -> GapCounts:
    """What the table records about one group. ``examples`` are question ids."""
    scores = [q.top_score for q in questions if q.top_score is not None]
    return GapCounts(
        centre=centre,
        question_count=len(questions),
        fallback_count=sum(q.fallback_used for q in questions),
        avg_top_score=sum(scores) / len(scores) if scores else None,
        negative_rate=sum(q.rating == -1 for q in questions) / len(questions),
        first_seen_at=min(q.asked_at for q in questions),
        last_seen_at=max(q.asked_at for q in questions),
        example_message_ids=examples,
    )


def _shown(text: str) -> str:
    """``text`` as the model is shown it: one line, clipped, with no double quote.

    A visitor's question can hold line breaks and quotation marks, and each question
    below sits on a line of its own between double quotes. Left as typed, a question
    could end its own line and start something that reads as the next one, or as
    an instruction. ``split()`` with no argument breaks on any run of whitespace,
    line breaks included, so joining the pieces with single spaces flattens it.
    """
    return " ".join(text.split()).replace('"', "'")[:SHOWN_LENGTH]


def brief(questions: list[FailedQuestion]) -> str:
    """What the model is shown to name a gap: each search, and the question behind it."""
    return "\n".join(
        f'{number}. searched "{_shown(q.query)}", asked "{_shown(q.question)}"'
        for number, q in enumerate(questions, start=1)
    )


def _fault(label: str) -> str | None:
    """Why ``label`` cannot be stored, in a word or two, or None when it can.

    ``str.isprintable()`` is False when a string holds any character that Unicode
    files under "Other" or "Separator", the plain space excepted: the control
    characters, NUL and ESC among them, and the invisible format characters that
    reverse or hide text. Postgres refuses NUL in a ``text`` column outright, and
    ESC begins the sequences that clear a terminal or recolour it. The API refuses
    control characters from visitors (``api/schemas.py``); a model's output is owed
    no more trust than theirs.
    """
    if not label:
        return "empty"
    if len(label) > MAX_LABEL:
        return "too long"
    if not label.isprintable():
        return "not plain text"
    if _LINK_OR_ADDRESS.search(label):
        return "a link or an address"
    return None


async def _name(questions: list[FailedQuestion]) -> tuple[str, Decimal]:
    """Ask the model for a label. Returns it with what the call cost."""
    settings = get_settings()
    result = await responses.parse(
        step="gap_label",
        # The classifier's model and effort: the cheap, fast one this project already
        # runs, doing the same kind of job, reading a little and choosing a few words.
        model=settings.classifier_model,
        effort=settings.classifier_reasoning_effort,
        instructions=GAP_LABEL.text,
        input=[{"role": "user", "content": brief(questions[:QUESTIONS_SHOWN])}],
        text_format=GapLabel,
        prompt=GAP_LABEL.ref,
    )
    label = " ".join(result.value.label.split()).rstrip(".") if result.value else ""
    # The prompt asks for sentence case, and the model does not always start with a
    # capital. Only the first letter is touched: "n8n" and "mihaylov.io" inside a
    # label are right as they are, which str.capitalize() would not leave alone.
    label = label[:1].upper() + label[1:]
    fault = _fault(label)
    if fault is not None:
        # A refusal, an unreadable answer, a paragraph where a title was asked for,
        # or something no title should hold. The gap is still real; it gets a
        # placeholder, and the next refresh asks again. The reason and the length
        # are logged, and never the label: it was written from visitors' questions.
        log.warning("gap_label_unusable", reason=fault, length=len(label))
        label = UNNAMED
    return label, result.usage.cost


async def _embed(failed: list[FailedQuestion]) -> tuple[list[list[float]], int, Decimal]:
    """A vector for each question's search, how many were embedded, and the cost.

    A set, so that a search several answers ran is embedded once; sorted, so that
    the texts and their vectors come back in one known order to be zipped together.
    """
    texts = sorted({question.query for question in failed})
    try:
        embedded = await embed_texts(texts)
    except APIError as exc:
        raise domain_error(exc) from exc
    by_text = dict(zip(texts, embedded.vectors, strict=True))
    return [by_text[question.query] for question in failed], len(texts), embedded.cost


async def _match(
    failed: list[FailedQuestion], vectors: list[list[float]], *, threshold: float
) -> list[Group]:
    """Cluster the searches, and say which stored gap each group is, if any."""
    # `taken` keeps two groups of one refresh from claiming the same stored gap: the
    # second would overwrite the first's counts.
    taken: list[int] = []
    groups: list[Group] = []

    for found in clustering.cluster(vectors, threshold=threshold):
        questions = [failed[index] for index in found.members]
        # The examples are the questions nearest the centre, the most typical ones.
        # Each member's likeness to the centre is worked out once, into a dictionary,
        # and sorted() is handed the dictionary's own lookup as its key: for each
        # member it calls alike[member]. The slice below then keeps the first few.
        alike = {index: cosine(vectors[index], found.centre) for index in found.members}
        typical = sorted(found.members, key=alike.__getitem__, reverse=True)
        counts = counts_for(
            questions, found.centre, [failed[index].question_id for index in typical[:EXAMPLES]]
        )

        match = await analytics_db.nearest_gap(found.centre, excluding=taken)
        if match is not None and match.similarity >= SAME_GAP:
            taken.append(match.id)
            groups.append(Group(questions, counts, gap_id=match.id, label=match.label))
        else:
            groups.append(Group(questions, counts, gap_id=None, label=None))

    return groups


def _wants_a_name(group: Group) -> bool:
    """A gap seen for the first time, or a stored one no model has named yet."""
    return group.gap_id is None or group.label == UNNAMED


async def _named(groups: list[Group]) -> tuple[list[Group], Decimal]:
    """Give the gaps that want one a label. Returns the groups, and what the naming cost.

    At most :data:`MAX_NAMED` are asked for. The groups arrive largest first, so the
    ones named are the most asked; the rest keep the placeholder until a later
    refresh reaches them.
    """
    cost = Decimal(0)
    asked = 0
    named: list[Group] = []
    for group in groups:
        if not _wants_a_name(group):
            named.append(group)
        elif asked == MAX_NAMED:
            named.append(dataclasses.replace(group, label=UNNAMED))
        else:
            label, spent = await _name(group.questions)
            asked += 1
            cost += spent
            named.append(dataclasses.replace(group, label=label))
    return named, cost


async def refresh(*, dry_run: bool = False) -> Refresh:
    """Work the gaps out again from what is stored, and update the table.

    ``dry_run`` clusters and matches and stops: no gap is named, so no model is
    asked, and nothing is written. It still embeds the searches, which costs a
    fraction of a cent.
    """
    settings = get_settings()
    # One more than is grouped, which is how this learns there were more. A slice
    # never raises: on a list shorter than the limit, [-MAX_QUESTIONS:] is all of it.
    failed = await analytics_db.failed_questions(
        low_score=settings.low_score_threshold, limit=MAX_QUESTIONS + 1
    )
    capped = len(failed) > MAX_QUESTIONS
    failed = failed[-MAX_QUESTIONS:]

    vectors, embedded, cost = await _embed(failed)
    groups = await _match(failed, vectors, threshold=settings.gap_similarity)
    if dry_run:
        # What the write below would empty: a stored gap that still has questions
        # and that no group turned out to be.
        matched = {group.gap_id for group in groups}
        would_empty = sum(
            gap.question_count > 0 and gap.id not in matched
            for gap in await analytics_db.list_gaps()
        )
        return Refresh(
            len(failed), groups, would_empty, embedded, cost, dry_run=True, capped=capped
        )

    # A stored gap gets a label written only if it had none. Worked out before the
    # naming, which replaces the placeholder this looks for.
    unnamed = {group.gap_id for group in groups if group.label == UNNAMED}

    # The names come before any write: a refresh that dies here leaves the table
    # exactly as it was.
    groups, spent = await _named(groups)
    emptied = await analytics_db.apply_gap_refresh(
        updates=[
            (group.gap_id, group.counts, group.label if group.gap_id in unnamed else None)
            for group in groups
            if group.gap_id is not None
        ],
        creates=[
            (group.label or UNNAMED, group.counts) for group in groups if group.gap_id is None
        ],
        refreshed_at=dt.datetime.now(dt.UTC),
    )

    result = Refresh(
        len(failed), groups, emptied, embedded, cost + spent, dry_run=False, capped=capped
    )
    # Counts and cost. Not the labels: they are written from visitors' questions.
    log.info(
        "gaps_refreshed",
        failed=result.failed,
        capped=result.capped,
        gaps=len(result.groups),
        created=len(result.created),
        updated=len(result.updated),
        emptied=result.emptied,
        unnamed=result.unnamed,
        cost_usd=str(result.cost),
    )
    return result
