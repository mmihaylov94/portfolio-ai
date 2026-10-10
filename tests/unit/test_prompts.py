"""The prompts: that they load, and that none of them changes by accident."""

import json
from pathlib import Path

import pytest

from portfolio_ai.assistant.prompts import loader
from portfolio_ai.assistant.prompts.loader import ALL, RAG_AGENT, Prompt

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
    "rag_agent": (2, "447fb30a2253"),
    "search_tool": (1, "e8ed50f414ec"),
    "out_of_scope_reply": (1, "915ed598f0a1"),
    "daily_limit_reply": (1, "f4daad636fed"),
    "judge": (2, "d8c0973fd84d"),
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


# The prompts that came from n8n. The rest were written here and have no original.
PORTED = ("classifier", "small_talk", "rag_agent", "search_tool", "out_of_scope_reply")

# n8n's lines that a later version replaced on purpose. Every other line of n8n's has to
# be there still, in order, so rewording a tuned line shows up twice in a diff: in the
# prompt, and here.
REWORDED: dict[str, tuple[str, ...]] = {
    # Version 2, after golden_v2's baseline: the three response style lines about length
    # and lists, and the answer pattern's last step. Adding lines beside them was
    # measured first and did not outweigh them. Rewording rule 9 and the whole "Answer
    # pattern" as well was measured too, and dropped: each fixed one fault and caused
    # another (docs/EVALS.md).
    "rag_agent": (
        "- Default answers should be 2\N{EN DASH}4 sentences.",
        "- Do not write long structured answers unless the user asks for more detail.",
        "- Prefer short paragraphs over bullet lists.",
        # gpt-6-luna read "optionally" literally, and never made the offer.
        "3. Optionally, one short sentence offering more details if the user is interested.",
    ),
}


def _n8n_originals() -> dict[str, str]:
    """n8n's own text for each ported prompt, read from the export."""
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
    return {name: text.strip() for name, text in originals.items()}


@pytest.mark.skipif(not N8N_EXPORT.exists(), reason="the n8n export is only on the dev machine")
def test_version_one_prompts_are_word_for_word_what_n8n_runs() -> None:
    """The port is verbatim, apart from the two corrections rag_agent.md lists.

    Only version-1 prompts that came from n8n are compared. Once a prompt is
    deliberately changed and its version bumped, the test below takes over; and a
    prompt written here, like the daily-limit reply, has no original at all.
    """
    originals = _n8n_originals()

    for prompt in ALL:
        if prompt.version == 1 and prompt.name in originals:
            assert prompt.text == originals[prompt.name], prompt.name


@pytest.mark.skipif(not N8N_EXPORT.exists(), reason="the n8n export is only on the dev machine")
@pytest.mark.parametrize(
    "prompt",
    [prompt for prompt in ALL if prompt.name in PORTED and prompt.version > 1],
    ids=lambda prompt: prompt.ref,
)
def test_a_changed_prompt_keeps_n8ns_lines_in_order_but_for_the_ones_listed(prompt: Prompt) -> None:
    """The classifier at version 2 only adds to n8n's text. The agent's prompt at
    version 2 also replaces the four lines listed in ``REWORDED``, and nothing else.

    The word-for-word test above stops looking at a prompt once its version moves, so
    this is what holds a changed one to what was decided.
    """
    original = _n8n_originals()[prompt.name].splitlines()
    replaced = REWORDED.get(prompt.name, ())
    kept = prompt.text.splitlines()

    # The list cannot go stale: each line on it was n8n's, and is no longer ours.
    for line in replaced:
        assert line in original, line
        assert line not in kept, line

    ours = iter(kept)
    for line in original:
        if line in replaced:
            continue
        # `in` on an iterator consumes it up to the match, so each line has to be
        # found after the one before it: the order is checked, not just presence.
        assert line in ours, line
