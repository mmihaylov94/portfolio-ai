"""Golden datasets: the questions an eval run asks, and what a right answer looks like.

A dataset is a YAML file in ``datasets/``, committed to the repository and read by a
person before it is trusted -- every score an eval run produces rests on it. This
module turns the file into validated, immutable objects, and refuses the file with
a message naming the problem when something in it is inconsistent.

Two Python details carry most of the weight here.

**Pydantic validates the YAML.** A YAML loader returns plain dictionaries and lists,
which will happily hold a misspelt field or a string where a list belongs.
``Dataset.model_validate`` checks every field against the models below, and
``extra="forbid"`` makes an unknown field an error rather than something silently
ignored -- ``expected_docs:`` for ``expected_doc_ids:`` would otherwise be a case
that tests nothing and passes.

**Tuples, and ``frozen=True``.** A case is an input to a measurement, and nothing
should be able to change one halfway through a run. Frozen models refuse attribute
assignment, and tuples instead of lists mean the collections inside them cannot be
appended to either.
"""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from portfolio_ai.assistant.classifier import Classification
from portfolio_ai.assistant.memory import HistoryMessage
from portfolio_ai.exceptions import EvalError

# Where `--dataset golden_v1` looks. Relative, so commands run from the repository
# root, like every other command in this project.
DATASETS_DIR = Path("datasets")

# "threadline-stack": lower-case words joined by hyphens, short enough to read in a
# report column.
Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=60)]
# "golden_v1", which also has to be the file's name.
Name = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$", max_length=60)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Turn(BaseModel):
    """One message of the conversation before a case's question."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: Text


class EvalCase(BaseModel):
    """One question, and everything that says whether it was answered well."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: Key
    question: Text
    # The route the classifier should choose.
    category: Classification
    # Other routes that also count as right. "Are you Mihail?" is answered properly
    # by small talk and by the knowledge-base route alike.
    also_accept: tuple[Classification, ...] = ()
    # The conversation so far, oldest first, for follow-ups like "yes please".
    history: tuple[Turn, ...] = ()
    # Every document that answers the question. Topics repeat across the knowledge
    # base -- contact details are in four documents -- so any one found is a hit.
    expected_doc_ids: tuple[str, ...] = ()
    # The knowledge base cannot answer this; saying so is the right answer.
    expect_fallback: bool = False
    # What a good answer says, written by a person. The judge scores completeness
    # against it.
    reference_answer: Text | None = None
    # Checked as case-insensitive substrings of the answer.
    must_include: tuple[Text, ...] = ()
    must_not_include: tuple[Text, ...] = ()
    notes: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """Rules that span fields, so no single field's type can express them.

        ``mode="after"`` runs this once every field has passed its own checks, on
        the finished object -- which is why it can read ``self.history`` knowing it
        is already a tuple of valid turns. Raising ``ValueError`` is how a validator
        says no; pydantic turns it into a validation error naming this case.
        """
        if self.category != "mihail_related" and self.expected_doc_ids:
            raise ValueError("only mihail_related questions search, so only they expect documents")
        if self.expect_fallback and self.expected_doc_ids:
            raise ValueError("a question the knowledge base cannot answer expects no documents")
        if self.expect_fallback and self.category != "mihail_related":
            raise ValueError("only a mihail_related question can expect the fallback answer")
        if self.category in self.also_accept:
            raise ValueError("also_accept repeats the category itself")
        if self.history:
            roles = [turn.role for turn in self.history]
            expected = ["user", "assistant"] * (len(roles) // 2)
            if roles != expected:
                raise ValueError("history alternates user and assistant, and ends with an answer")
        return self

    @property
    def accepted_routes(self) -> frozenset[str]:
        return frozenset((self.category, *self.also_accept))

    def history_messages(self) -> list[HistoryMessage]:
        """The history in the shape ``agent.respond()`` takes."""
        return [HistoryMessage(role=turn.role, content=turn.content) for turn in self.history]


class Dataset(BaseModel):
    """A named, versioned set of cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Name
    description: Text
    cases: tuple[EvalCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [case.key for case in self.cases]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"case keys must be unique; repeated: {', '.join(duplicates)}")
        return self

    @property
    def content_hash(self) -> str:
        """A hash of the cases, which is what freezes a dataset once it has a complete run.

        ``sort_keys`` and fixed separators make the JSON canonical: the same cases
        always give the same text, whatever order the YAML listed a case's fields in.
        The description is left out on purpose -- rewording it changes no score.

        ``exclude_defaults`` leaves out every field still at its default. Without it
        the hash would depend on the code as well as the file: give ``EvalCase`` one
        new optional field and every dump gains ``"new_field": null``, so every frozen
        dataset would read as changed and be refused. With it, only what the file
        actually says is hashed.
        """
        cases = [case.model_dump(mode="json", exclude_defaults=True) for case in self.cases]
        canonical = json.dumps(cases, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _StrictLoader(yaml.SafeLoader):
    """``yaml.safe_load``'s loader, except that a key written twice is an error.

    Plain YAML keeps the last of two equal keys without a word, so a case with
    ``expected_doc_ids`` written twice would quietly be scored on the second, and
    ``extra="forbid"`` never sees the first. In a file edited by hand, that is a
    matter of time.
    """


def _mapping_without_repeats(loader: yaml.SafeLoader, node: yaml.Node) -> dict[object, object]:
    if not isinstance(node, yaml.MappingNode):  # pragma: no cover - registered for mappings
        raise yaml.constructor.ConstructorError(None, None, "expected a mapping", node.start_mark)
    keys = [loader.construct_object(key) for key, _ in node.value]
    repeated = sorted({key for key in keys if isinstance(key, str) and keys.count(key) > 1})
    if repeated:
        raise yaml.constructor.ConstructorError(
            None, None, f"the key {repeated[0]!r} appears twice", node.start_mark
        )
    return loader.construct_mapping(node)


# Every mapping this loader builds goes through the check above.
_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_repeats
)


def path_for(dataset: str) -> Path:
    """``golden_v1`` -> ``datasets/golden_v1.yaml``; a path is used as given."""
    if dataset.endswith((".yaml", ".yml")) or "/" in dataset or "\\" in dataset:
        return Path(dataset)
    return DATASETS_DIR / f"{dataset}.yaml"


def load(path: Path) -> Dataset:
    """Read and validate a dataset file. Any problem is an ``EvalError`` naming it."""
    try:
        text = path.read_text(encoding="utf-8")
        # ruff cannot see that _StrictLoader is a SafeLoader, so it warns as it would
        # for the unsafe loader that can build arbitrary Python objects. It cannot.
        raw = yaml.load(text, Loader=_StrictLoader)  # ruff: ignore[unsafe-yaml-load]
    except OSError as exc:
        raise EvalError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise EvalError(f"{path} is not valid YAML: {exc}") from exc

    try:
        dataset = Dataset.model_validate(raw)
    except ValidationError as exc:
        # The message lists every problem with its location -- cases.12.category,
        # say -- so it goes out whole.
        raise EvalError(f"{path} is not a valid dataset.\n{exc}") from exc

    # The file name is how a dataset is found (--dataset golden_v1), and the name
    # inside is what runs are stored under. If they differed, the same file could be
    # seeded under two names.
    if dataset.name != path.stem:
        raise EvalError(f"{path}: the dataset is named {dataset.name!r}; it must match the file")

    return dataset
