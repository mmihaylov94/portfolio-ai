"""Promote a visitor's question into an eval case.

The last link of the loop: a question the knowledge base could not answer became a
content gap, an article was written to close it, and now the question itself becomes
a test, so that the assistant cannot quietly get worse at what it was fixed for.

**The case is printed, not stored.** Eval cases live where evals run, which is the
development database, and the question is in production's. So ``promote-case`` runs
where the question is and prints one case as YAML. A person reads it, rewrites the
question in their own words, pastes it into ``datasets/promoted.yaml`` in the
repository, and it joins the next golden dataset.

**It is built as an** :class:`~portfolio_ai.evals.datasets.EvalCase`, the model the
eval harness loads a dataset into, and dumped from that. What is printed has
therefore already passed every rule a dataset file is checked against, and the two
cannot drift apart: a field the harness gains or a rule it tightens applies here
with no change to this module.

**What is printed is a visitor's words**, and a commit to a public repository is for
good, where the chat tells visitors their conversations are deleted after 90 days.
The command is for a terminal, like ``report`` and ``gaps``, and what it prints ends
with what to do before committing: first of all, to rewrite the question.
"""

import textwrap
from collections.abc import Sequence

import yaml
from pydantic import TypeAdapter, ValidationError

from portfolio_ai.config import get_settings
from portfolio_ai.db import analytics as analytics_db
from portfolio_ai.evals.datasets import HOLDING, EvalCase, Key, path_for
from portfolio_ai.exceptions import AnalyticsError

# What a case is called until a person names it.
KEY_PREFIX = "promoted-"

# U+0085, "next line": a line break few things write, which the API lets through in
# a message and which YAML reads as a line break and folds away. When the review of
# this stage tried every character the API accepts, it was the one that did not come
# back from the file as it went in. Written as an escape so that this file does not
# hold one.
_NEXT_LINE = "\x85"

# pydantic validates a model's fields. A TypeAdapter does the same for a type that
# stands alone, here the annotated string a case's key has to be, so that a key can
# be checked before there is a case to put it in.
_KEY = TypeAdapter(Key)


def default_key(question_id: int) -> str:
    """``promoted-812``: a key that is valid and says nothing, to be replaced."""
    return f"{KEY_PREFIX}{question_id}"


def _as_typed(text: str) -> str:
    """A stored message as a case can carry it: U+0085 becomes the line break it means."""
    return text.replace(_NEXT_LINE, "\n")


async def promote(
    message_id: int, *, expected_docs: Sequence[str], key: str | None, with_history: bool
) -> EvalCase:
    """The eval case for one stored question. Reads the database and writes nothing.

    The case's category is ``mihail_related`` whatever route the question took. It
    names documents that answer it, so that is the route it should take; a question
    that was turned away as off-topic and is promoted is a test of exactly that.
    """
    if key is not None:
        # Before anything is read, so that a wrong key and a wrong id are both found
        # in one run and not in two.
        try:
            _KEY.validate_python(key)
        except ValidationError as exc:
            raise AnalyticsError(
                "--key takes lower-case words joined by hyphens, at most 60 characters: "
                "day-rate-pricing."
            ) from exc

    asked = await analytics_db.answered_question(message_id)
    if asked is None:
        raise AnalyticsError(
            f"No answered question has the id {message_id}. `report` and `gaps` print "
            "the ids of the questions still stored."
        )

    # dict.fromkeys keeps the first of each and the order they came in: a doc_id given
    # twice on the command line is listed once.
    docs = list(dict.fromkeys(expected_docs))
    missing = [doc for doc in docs if not await analytics_db.document_exists(doc)]
    if missing:
        raise AnalyticsError(
            f"No indexed article has the doc_id {', '.join(repr(doc) for doc in missing)}. "
            "A case that expects a document nothing has indexed can only fail."
        )

    history: list[dict[str, str]] = []
    if with_history:
        # As many exchanges as the assistant itself was given when it answered.
        before = await analytics_db.exchanges_before(
            asked.session_pk, asked.question_id, limit=get_settings().memory_window_turns
        )
        for question, answer in before:
            history.append({"role": "user", "content": _as_typed(question)})
            history.append({"role": "assistant", "content": _as_typed(answer)})

    happened = f"routed as {asked.route}"
    if asked.fallback_used:
        happened += " and answered with the fallback"
    try:
        # model_validate, on plain dictionaries and lists, and not EvalCase(...) with
        # Turn objects made beforehand: every value then goes through one check, in
        # one place, and a fault anywhere is reported the same way.
        return EvalCase.model_validate(
            {
                "key": key or default_key(asked.question_id),
                "question": _as_typed(asked.question),
                "category": "mihail_related",
                "history": history,
                "expected_doc_ids": docs,
                "notes": (
                    f"Promoted from a visitor's question (message {asked.question_id}, asked "
                    f"{asked.asked_at:%Y-%m-%d}). It was {happened}."
                ),
            }
        )
    except ValidationError as exc:
        # Which fields, and no more: `errors()` gives each problem as a dictionary,
        # and its "loc" is where it is. Not str(exc), which quotes the value that was
        # refused. Here that is a visitor's words, and an error is a log line.
        problems = exc.errors(include_input=False, include_url=False)
        fields = sorted({str(problem["loc"][0]) for problem in problems if problem["loc"]})
        raise AnalyticsError(
            f"That does not make a valid eval case: {', '.join(fields) or 'the case'}. If it "
            "is the history, a stored message in it is empty: promote the question without "
            "--with-history."
        ) from exc


def as_yaml(case: EvalCase) -> str:
    """The case as one item of a dataset's ``cases:`` list, indented to sit under it.

    ``model_dump`` turns the case, and the turns inside it, into plain dictionaries,
    and ``mode="json"`` makes every value in them something JSON could hold. That is
    what ``yaml.safe_dump`` writes: it refuses an object it has no plain form for,
    which is the point of the "safe" in its name. ``exclude_defaults`` leaves out
    every field the case does not set, as a hand-written case leaves them out.
    ``sort_keys=False`` keeps the fields in the order the model declares them,
    ``allow_unicode`` writes an accented letter as itself and not as an escape, and
    ``width`` is where a long line is folded.
    """
    data = case.model_dump(mode="json", exclude_defaults=True)
    dumped = yaml.safe_dump([data], sort_keys=False, allow_unicode=True, width=88)
    return textwrap.indent(dumped, "  ")


def what_to_do(case: EvalCase) -> str:
    """What follows the case: where it goes, and what to do before it is committed.

    Every line is a YAML comment, so that the whole of the command's output can be
    pasted into the file and the file still loads.
    """
    holding = path_for(HOLDING).as_posix()
    rewrite = "the question and the history" if case.history else "the question"
    key = (
        "Give it a key that says what it tests."
        if case.key.startswith(KEY_PREFIX)
        else "Check that its key says what it tests."
    )
    lines = [
        f"One case, to go under `cases:` in {holding}.",
        'docs/EVALS.md, "Promoted cases", has the file\'s header if this is its first.',
        "",
        "Before it is committed. The repository is public, a commit is for good, and",
        "the chat tells visitors their conversations are deleted after 90 days:",
        f"  1. Rewrite {rewrite} in your own words. Keep what is asked, and",
        "     how plainly. Leave out the visitor's phrasing, and anything that says who",
        "     asked: a name, an employer, a place, an address.",
        "  2. Write its reference_answer from the article. Without one the judge has",
        "     nothing to grade completeness against.",
        f"  3. {key}",
        "",
        f"Then: python -m portfolio_ai.evals check {holding}",
    ]
    return "\n".join(f"# {line}".rstrip() for line in lines)
