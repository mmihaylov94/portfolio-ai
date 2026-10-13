"""YAML read strictly: a key written twice in one mapping is an error.

Two kinds of file in this project are written by hand and trusted as written: the
eval datasets and the invented conversations. Plain YAML keeps the last of two equal
keys without a word. A case with ``expected_doc_ids`` written twice would quietly be
scored on the second, and ``extra="forbid"`` on the model never sees the first,
because by the time pydantic is handed the dictionary there is only one. In a file
edited by hand, that is a matter of time.

The loader is a subclass of ``yaml.SafeLoader`` with one constructor replaced: the
one that builds a mapping. Subclassing keeps the change here. Registering the
constructor on ``SafeLoader`` itself would change ``yaml.safe_load`` for every caller
in the process, the prompt loader's included.
"""

from typing import Any

import yaml


class _Loader(yaml.SafeLoader):
    """``yaml.safe_load``'s loader, except that a key written twice is an error."""


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
_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_repeats)


def load(text: str) -> Any:
    """Parse ``text``. Raises ``yaml.YAMLError`` for malformed YAML or a repeated key.

    ``Any``, because YAML can hold anything: the caller validates what came back.
    """
    # ruff cannot see that _Loader is a SafeLoader, so it warns as it would for the
    # unsafe loader that can build arbitrary Python objects. It cannot.
    return yaml.load(text, Loader=_Loader)  # ruff: ignore[unsafe-yaml-load]
