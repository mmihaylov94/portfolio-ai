"""The report: what was asked in a window, and where the answers fell short.

Two steps, kept apart so that each can be tested without the other.
:func:`build` reads the database and embeds the window's questions; what it returns
is a :class:`Report`, which is only data. :func:`render` turns a report into text and
touches nothing.

``render`` has two modes, and the difference between them is a privacy rule and
not a matter of length. With ``verbatim=True`` it quotes what visitors typed: that
is for a terminal, read by the site's owner and then gone. With ``verbatim=False``
it gives the same counts and not one word a visitor wrote, and that is the only
form allowed to leave the database's side of things, by email or into a log.
The chat tells visitors their conversations are kept for 90 days; a copy quoted
into a mailbox would outlive that.
"""

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

import structlog
from openai import APIError

from portfolio_ai.analytics import signals
from portfolio_ai.analytics.signals import Pair
from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.db import documents as docs_db
from portfolio_ai.db.analytics import ArticleUsage, Turn, Volume
from portfolio_ai.exceptions import AnalyticsError
from portfolio_ai.llm.embeddings import embed_texts
from portfolio_ai.llm.responses import domain_error

log = structlog.get_logger(__name__)

# An article counts as used by an answer when one of its sections was among this
# many of the first search's best passages. Five of the twenty a search returns:
# few enough to mean "this is what the answer was probably written from".
BEST_PASSAGES = 5

# Below this many counted answers, which sections were never used says more about
# how little was asked than about the sections.
MIN_ANSWERS_FOR_UNUSED = 50

ROUTE_NAMES = {
    "mihail_related": "about Mihail",
    "small_talk": "small talk",
    "out_of_scope": "off-topic",
}

_SINCE = re.compile(r"(\d{1,4})d")


def window(since: str, *, now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    """``"7d"`` as a pair of timestamps: seven days ago, and now."""
    match = _SINCE.fullmatch(since.strip())
    if match is None or int(match[1]) < 1:
        raise AnalyticsError(f"--since takes a number of days, such as 7d or 30d, not {since!r}.")
    until = now or dt.datetime.now(dt.UTC)
    return until - dt.timedelta(days=int(match[1])), until


@dataclass(frozen=True)
class Refused:
    """A question sent away as off-topic, with the passage that was nearest to it."""

    turn: Turn
    score: float | None
    title: str | None
    section: str | None


@dataclass(frozen=True)
class Report:
    """Everything one report says, before any of it is text."""

    since: dt.datetime
    until: dt.datetime
    turns: list[Turn]
    # The window of the same length just before this one.
    previous: Volume
    # Every question paired with the one before it in its conversation.
    pairs: list[Pair]
    refused: list[Refused]
    usage: ArticleUsage
    # The two thresholds the lists below were drawn with, so that a report says
    # what it was measured against.
    low_score_threshold: float
    rephrase_similarity: float
    # What building this report asked of OpenAI: how many questions were embedded,
    # and what that cost. Printed with the report, because the command runs at a log
    # level that drops the line the embeddings call writes, and this project
    # records what every OpenAI call cost.
    embedded: int
    embedding_tokens: int
    embedding_cost: Decimal

    @property
    def conversations(self) -> int:
        return len({turn.conversation for turn in self.turns})

    @property
    def knowledge_base(self) -> list[Turn]:
        """The turns that searched: everything routed as being about Mihail."""
        return [turn for turn in self.turns if turn.route == "mihail_related"]

    @property
    def fallbacks(self) -> list[Turn]:
        return [turn for turn in self.knowledge_base if turn.fallback_used]

    @property
    def thin(self) -> list[Turn]:
        """Answered, with nothing close to answer from, lowest score first.

        A fallback with a low score is the assistant working as intended and is
        already counted above. This is the other case, and the one the score is
        kept for: no passage came close, and an answer was written anyway.
        """
        found = [
            turn
            for turn in self.knowledge_base
            if not turn.fallback_used
            and turn.top_score is not None
            and turn.top_score < self.low_score_threshold
        ]
        return sorted(found, key=lambda turn: turn.top_score or 0.0)

    @property
    def asked_again(self) -> list[Pair]:
        return [pair for pair in self.pairs if pair.similarity >= self.rephrase_similarity]

    @property
    def thumbs_down(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.rating == -1]

    @property
    def thumbs_up(self) -> int:
        return sum(turn.rating == 1 for turn in self.turns)

    @property
    def cost(self) -> Decimal:
        # sum() starts from the int 0 unless told otherwise, and an empty window
        # would then return an int where everything else expects a Decimal.
        return sum((turn.cost for turn in self.turns), start=Decimal(0))


async def build(*, since: dt.datetime, until: dt.datetime) -> Report:
    """Read the window and work out its signals. The only spend is embedding its questions.

    The questions are embedded here, each time, and the vectors are thrown away
    afterwards. Storing them would mean a table to keep in step with the purge;
    embedding a few hundred short questions costs a fraction of a cent, so there is
    nothing to save. ``embed_texts`` sends them ``EMBEDDING_BATCH_SIZE`` at a time,
    64 by default, so a quiet week is one request and three busy months are several.
    """
    settings = get_settings()

    turns = await analytics_db.turns_between(since, until)
    previous = await analytics_db.volume_between(since - (until - since), since)
    usage = await analytics_db.article_usage(since, until, best=BEST_PASSAGES)

    # A set, so that a question asked by three visitors is embedded once; sorted, so
    # that the order of the texts and the order of their vectors is one known order.
    texts = sorted({turn.question for turn in turns if turn.route != "small_talk"})
    try:
        embedded = await embed_texts(texts)
    except APIError as exc:
        raise domain_error(exc) from exc
    vectors = dict(zip(texts, embedded.vectors, strict=True))

    refused: list[Refused] = []
    for turn in turns:
        if turn.route != "out_of_scope":
            continue
        nearest = await docs_db.search_chunks(vectors[turn.question], top_k=1)
        best = nearest[0] if nearest else None
        refused.append(
            Refused(
                turn=turn,
                score=best.score if best else None,
                title=best.title if best else None,
                section=(best.section_title or best.section) if best else None,
            )
        )
    # The likeliest misroutes first: a question refused as off-topic that sits close
    # to a passage was probably about Mihail after all.
    refused.sort(key=lambda item: item.score or 0.0, reverse=True)

    report = Report(
        since=since,
        until=until,
        turns=turns,
        previous=previous,
        pairs=signals.consecutive_pairs(turns, vectors),
        refused=refused,
        usage=usage,
        low_score_threshold=settings.low_score_threshold,
        rephrase_similarity=settings.rephrase_similarity,
        embedded=len(texts),
        embedding_tokens=embedded.total_tokens,
        embedding_cost=embedded.cost,
    )

    # Counts, never a question.
    log.info(
        "report_built",
        questions=len(report.turns),
        conversations=report.conversations,
        fallbacks=len(report.fallbacks),
        asked_again=len(report.asked_again),
        embedded=report.embedded,
        embedding_tokens=report.embedding_tokens,
        embedding_cost_usd=str(report.embedding_cost),
    )
    return report


# --- rendering ------------------------------------------------------------------


def _clip(text: str, limit: int = 160) -> str:
    """One line of at most ``limit`` characters. ``split()`` with no argument breaks
    on any run of whitespace, newlines included, so joining its pieces flattens a
    pasted paragraph."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _score(value: float | None) -> str:
    """Three decimals, where the thresholds are shown with two. A score of 0.348 is
    under a threshold of 0.35; printed as 0.35 it would sit in a list headed "under
    0.35" looking as if it did not belong there."""
    return f"{value:.3f}" if value is not None else "  -- "


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / 1000:.1f}s"


def _days(report: Report) -> int:
    return max(1, round((report.until - report.since) / dt.timedelta(days=1)))


def _volume(report: Report) -> list[str]:
    days = _days(report)
    routes = Counter(turn.route for turn in report.turns)
    counts = [f"{routes[route]} {name}" for route, name in ROUTE_NAMES.items() if routes[route]]
    # A route this module has no name for. There is none today; a fourth route added
    # to the classifier would otherwise drop out of this line without a word.
    other = len(report.turns) - sum(routes[route] for route in ROUTE_NAMES)
    if other:
        counts.append(f"{other} routed some other way")
    by_route = ", ".join(counts)
    return [
        "## Volume",
        "",
        f"- {_plural(len(report.turns), 'question')} in "
        f"{_plural(report.conversations, 'conversation')} "
        f"(the {_plural(days, 'day')} before: {report.previous.questions} in "
        f"{report.previous.conversations})",
        f"- {by_route}",
    ]


def _shortfalls(report: Report, *, verbatim: bool) -> list[str]:
    asked = len(report.knowledge_base)
    declined = len(report.fallbacks)
    share = f" ({declined / asked:.0%})" if asked else ""
    lines = [
        "## Where answers fell short",
        "",
        f"- {declined} of {_plural(asked, 'knowledge-base answer')} said the information "
        f"was not there{share}",
        f"- {len(report.thin)} answered with nothing close "
        f"(best passage under {report.low_score_threshold:.2f})",
        f"- {_plural(len(report.asked_again), 'question')} asked again in other words "
        f"(similarity {report.rephrase_similarity:.2f} or more)",
        f"- {report.thumbs_up} thumbs up, {len(report.thumbs_down)} thumbs down",
        f"- {len(report.refused)} refused as off-topic",
    ]
    if not verbatim:
        return lines

    def listed(title: str, entries: list[list[str]], note: str = "") -> None:
        if not entries:
            return
        lines.extend(["", f"### {title}", ""])
        if note:
            lines.extend([note, ""])
        for entry in entries:
            lines.extend(entry)

    listed(
        "Said the information was not there",
        [
            [f"- #{turn.question_id}  {_score(turn.top_score)}  {_clip(turn.question)}"]
            for turn in sorted(report.fallbacks, key=lambda turn: turn.top_score or 0.0)
        ],
        "Each with the score of its best passage, lowest first.",
    )
    listed(
        "Answered with nothing close",
        [
            [
                f"- #{turn.question_id}  {_score(turn.top_score)}  {_clip(turn.question)}",
                f"    answered: {_clip(turn.answer)}",
            ]
            for turn in report.thin
        ],
    )
    listed(
        "Asked again in other words",
        [
            [
                f"- #{pair.first.question_id} then #{pair.second.question_id}  "
                f"{pair.similarity:.2f}",
                f"    {_clip(pair.first.question)}",
                f"    {_clip(pair.second.question)}",
            ]
            for pair in sorted(report.asked_again, key=lambda pair: pair.similarity, reverse=True)
        ],
    )
    listed(
        "Thumbs down",
        [
            [
                f"- #{turn.question_id}  {_score(turn.top_score)}  {_clip(turn.question)}",
                *([f"    comment: {_clip(turn.comment)}"] if turn.comment else []),
                f"    answered: {_clip(turn.answer)}",
            ]
            for turn in report.thumbs_down
        ],
    )
    listed(
        "Refused as off-topic",
        [
            [
                f"- #{item.turn.question_id}  {_score(item.score)}  {_clip(item.turn.question)}",
                f'    nearest: {item.title}, "{item.section}"'
                if item.title
                else "    nearest: nothing indexed",
            ]
            for item in report.refused
        ],
        "Nearest passage first. A high score here is a question that was probably "
        "about Mihail and was turned away.",
    )
    return lines


def _articles(report: Report, *, verbatim: bool) -> list[str]:
    usage = report.usage
    intro = (
        f"How many answers had a section of each among the {BEST_PASSAGES} best passages "
        "of their first search."
    )
    if usage.counted < usage.searched:
        intro += (
            f" {usage.counted} of {_plural(usage.searched, 'answer')} that searched are "
            "counted: the rest were stored before answers recorded which article each "
            "passage came from."
        )
    lines = ["## Articles", "", intro, ""]
    # The email gets the articles that were used; the terminal gets all of them, the
    # unused ones being the point of looking.
    articles = usage.articles if verbatim else [a for a in usage.articles if a.answers]
    enough = usage.counted >= MIN_ANSWERS_FOR_UNUSED
    for article in articles:
        sections = (
            f"{article.sections_used} of {_plural(article.sections, 'section')} used"
            if verbatim and enough
            else _plural(article.sections, "section")
        )
        lines.append(f"- {article.answers:>4}  {article.title} ({article.doc_id}), {sections}")
        if verbatim and enough and article.unused:
            # Section headings, which are the knowledge base's words and nobody's
            # question.
            named = ", ".join(f'"{heading}"' for heading in article.unused)
            lines.append(f"        never among the best {BEST_PASSAGES}: {named}")
    if not articles:
        lines.append("- none")
    if verbatim and not enough:
        lines.extend(
            [
                "",
                f"Sections nobody reached are not named until {MIN_ANSWERS_FOR_UNUSED} "
                f"answers are counted ({usage.counted} so far).",
            ]
        )
    return lines


def _cost_and_speed(report: Report) -> list[str]:
    answers = len(report.turns)
    each = report.cost / answers if answers else Decimal(0)
    lines = [
        "## Cost and speed",
        "",
        f"- ${report.cost:.4f} for {_plural(answers, 'answer')} (${each:.4f} each)",
    ]
    searched = report.knowledge_base
    first = signals.spread([t.first_token_ms for t in searched if t.first_token_ms is not None])
    whole = signals.spread([t.latency_ms for t in searched if t.latency_ms is not None])
    if first:
        lines.append(
            f"- First word of a knowledge-base answer: median {_seconds(first.median)}, "
            f"nine in ten within {_seconds(first.p90)}"
        )
    if whole:
        lines.append(
            f"- Whole answer: median {_seconds(whole.median)}, "
            f"nine in ten within {_seconds(whole.p90)}"
        )
    lines.append(
        f"- This report: {_plural(report.embedded, 'question')} embedded, "
        f"{report.embedding_tokens} tokens, ${report.embedding_cost:.6f}"
    )
    return lines


def _distributions(report: Report) -> list[str]:
    lines = [
        "## For setting the thresholds",
        "",
        "Best passage score of each knowledge-base answer "
        f"(LOW_SCORE_THRESHOLD is {report.low_score_threshold:.2f}):",
        "",
    ]
    scored = [turn for turn in report.knowledge_base if turn.top_score is not None]
    declined = dict(
        signals.histogram((t.top_score or 0.0 for t in scored if t.fallback_used), width=0.05)
    )
    for low, count in signals.histogram((t.top_score or 0.0 for t in scored), width=0.05):
        said_no = declined.get(low, 0)
        note = f"  ({said_no} said the information was not there)" if said_no else ""
        lines.append(f"  {low:.2f} to {low + 0.05:.2f}  {count:>4}{note}")
    if not scored:
        lines.append("  nothing searched in this window")

    lines.extend(
        [
            "",
            "Similarity of each question to the one before it in its conversation "
            f"(REPHRASE_SIMILARITY is {report.rephrase_similarity:.2f}):",
            "",
        ]
    )
    for low, count in signals.histogram((pair.similarity for pair in report.pairs), width=0.1):
        lines.append(f"  {low:.1f} to {low + 0.1:.1f}  {count:>4}")
    if not report.pairs:
        lines.append("  no two questions in a row in this window")
    return lines


def render(report: Report, *, verbatim: bool) -> str:
    """The report as Markdown.

    ``verbatim=False`` is the form that may be emailed or logged: counts, article
    titles, costs and timings, and nothing a visitor typed. Keep it that way. A
    test asserts it by rendering a report and looking for every question, answer
    and comment in the result.
    """
    title = (
        f"# Chat report: {report.since:%Y-%m-%d} to {report.until:%Y-%m-%d} "
        f"({_plural(_days(report), 'day')})"
    )
    if not report.turns:
        return "\n".join(
            [
                title,
                "",
                "Nobody asked anything in this window. The "
                f"{_plural(_days(report), 'day')} before: "
                f"{_plural(report.previous.questions, 'question')} in "
                f"{_plural(report.previous.conversations, 'conversation')}.",
            ]
        )

    sections = [
        [title],
        _volume(report),
        _shortfalls(report, verbatim=verbatim),
        _articles(report, verbatim=verbatim),
        _cost_and_speed(report),
    ]
    if verbatim:
        sections.append(_distributions(report))
    return "\n\n".join("\n".join(section) for section in sections)
