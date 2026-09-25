"""The prompts: that they load, and that none of them changes by accident."""

import json
from pathlib import Path

import pytest

from portfolio_ai.assistant.prompts import loader
from portfolio_ai.assistant.prompts.loader import ALL, CLASSIFIER, RAG_AGENT, Prompt

# Each prompt's version and a hash of its text. This is the test that fails when a
# prompt is edited, and it is meant to.
#
# If you changed a prompt on purpose: bump `version` in its frontmatter, then update
# its line here with the new digest (the failure message prints it). Both edits land
# in the same diff, which is the point -- a change to a tuned prompt becomes visible
# in review instead of arriving silently. A changed prompt should also come with an
# eval run showing it helped; see CLAUDE.md.
PINNED = {
    "classifier": (2, "2397208265ab"),
    "small_talk": (1, "4e7ec2947c35"),
    "rag_agent": (1, "7cbb3c6d8e57"),
    "search_tool": (1, "e8ed50f414ec"),
    "out_of_scope_reply": (1, "915ed598f0a1"),
    "daily_limit_reply": (1, "f4daad636fed"),
    "judge": (1, "17a5b3808a98"),
}

# The n8n export the prompts were ported from. Gitignored -- it carries n8n instance
# and credential ids -- so it exists on the development machine and nowhere else,
# and the test that reads it skips everywhere else, CI included.
N8N_EXPORT = (
    Path(__file__).resolve().parents[2]
    / "n8n_workflows"
    / "Portfolio _ AI Chat -_ RAG Vector Store.json"
)


def test_every_prompt_is_pinned() -> None:
    assert sorted(prompt.name for prompt in ALL) == sorted(PINNED)


@pytest.mark.parametrize("prompt", ALL, ids=lambda prompt: prompt.name)
def test_a_prompt_cannot_change_without_a_new_version(prompt: Prompt) -> None:
    expected_version, expected_digest = PINNED[prompt.name]

    assert (prompt.version, prompt.digest) == (expected_version, expected_digest), (
        f"{prompt.name}.md changed. If that was deliberate, bump its version and pin "
        f'"{prompt.name}": ({prompt.version}, "{prompt.digest}") in this file.'
    )


@pytest.mark.parametrize("prompt", ALL, ids=lambda prompt: prompt.name)
def test_no_prompt_starts_with_n8ns_expression_marker(prompt: Prompt) -> None:
    assert not prompt.text.startswith("=")


def test_the_agent_prompt_names_the_tool_the_model_can_actually_call() -> None:
    assert '"search_knowledgebase"' in RAG_AGENT.text
    assert "Postgres | RAG Knowledgebase" not in RAG_AGENT.text


def test_ref_names_the_prompt_and_its_version() -> None:
    assert RAG_AGENT.ref == f"rag_agent@{RAG_AGENT.version}"


def test_a_prompt_without_a_version_is_refused() -> None:
    with pytest.raises(ValueError, match="version"):
        loader.parse("broken", "---\nsource: somewhere\n---\n\nSome text.\n")


def test_a_boolean_is_not_a_version() -> None:
    # YAML reads `true` as a bool, and bool is a subclass of int in Python -- so
    # without the explicit check, `version: true` would be accepted as version 1.
    with pytest.raises(ValueError, match="version"):
        loader.parse("broken", "---\nversion: true\n---\n\nSome text.\n")


def test_an_empty_prompt_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        loader.parse("broken", "---\nversion: 1\n---\n\n   \n")


@pytest.mark.skipif(not N8N_EXPORT.exists(), reason="the n8n export is only on the dev machine")
def test_version_one_prompts_are_word_for_word_what_n8n_runs() -> None:
    """The port is verbatim, apart from the two corrections rag_agent.md lists.

    Only version-1 prompts that came from n8n are compared. Once a prompt is
    deliberately changed and its version bumped, it has left n8n behind on purpose;
    and a prompt written here, like the daily-limit reply, has no original at all.
    """
    nodes = {
        node["name"]: node["parameters"]
        for node in json.loads(N8N_EXPORT.read_text(encoding="utf-8"))["nodes"]
    }
    rag = nodes["AI | RAG Agent"]["options"]["systemMessage"]
    originals = {
        "classifier": nodes["AI | Classify Message"]["options"]["systemPromptTemplate"],
        "small_talk": nodes["AI | Small Talk Responses"]["options"]["systemMessage"],
        # The two recorded corrections, applied to the original.
        "rag_agent": rag.removeprefix("=").replace(
            '"Postgres | RAG Knowledgebase"', '"search_knowledgebase"'
        ),
        "search_tool": nodes["Postgres | RAG Knowledgebase"]["toolDescription"],
        "out_of_scope_reply": nodes["Set | Out of Scope Reply"]["assignments"]["assignments"][0][
            "value"
        ],
    }

    for prompt in ALL:
        if prompt.version == 1 and prompt.name in originals:
            assert prompt.text == originals[prompt.name].strip(), prompt.name


@pytest.mark.skipif(not N8N_EXPORT.exists(), reason="the n8n export is only on the dev machine")
def test_the_classifier_keeps_every_line_of_n8ns_prompt_in_order() -> None:
    """Version 2 adds the project names, a rule and examples, and removes nothing.

    The word-for-word test above stops looking at a prompt once its version moves,
    so this is what holds the classifier to "only added to".
    """
    nodes = {
        node["name"]: node["parameters"]
        for node in json.loads(N8N_EXPORT.read_text(encoding="utf-8"))["nodes"]
    }
    original = nodes["AI | Classify Message"]["options"]["systemPromptTemplate"].strip()
    ours = iter(CLASSIFIER.text.splitlines())

    for line in original.splitlines():
        # `in` on an iterator consumes it up to the match, so each line has to be
        # found after the one before it: the order is checked, not just presence.
        assert line in ours, line
