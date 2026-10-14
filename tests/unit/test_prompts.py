"""The prompts: that they load, and that none of them changes by accident."""

import json
import re
from pathlib import Path

import pytest

from portfolio_ai.assistant.prompts import loader
from portfolio_ai.assistant.prompts.loader import ALL, EVERY, RAG_AGENT, Prompt

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
    "gap_label": (1, "b3b22eb0b65c"),
    "contact_classifier": (1, "b71c9fea9de0"),
    "contact_reply": (1, "c2ace85bac9d"),
    "contact_context": (1, "a2bce9f1a0ff"),
    "contact_brief_project": (1, "5ec5abffa9f2"),
    "contact_brief_collaboration": (1, "e86ca5118675"),
    "contact_brief_job": (1, "df861500bb99"),
    "contact_brief_consultation": (1, "4e99b052632a"),
    "contact_brief_other": (1, "5cd3015fbcca"),
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
    """EVERY, not ALL. ALL is the prompts an answer or a grade depends on, which is
    what an eval run records; EVERY adds the ones that do neither, and a prompt is
    pinned whichever it is."""
    assert sorted(prompt.name for prompt in EVERY) == sorted(PINNED)


@pytest.mark.parametrize("prompt", EVERY, ids=lambda prompt: prompt.name)
def test_a_prompt_cannot_change_without_a_new_version(prompt: Prompt) -> None:
    expected_version, expected_digest = PINNED[prompt.name]

    assert (prompt.version, prompt.digest) == (expected_version, expected_digest), (
        f"{prompt.name}.md changed. If that was deliberate, bump its version and pin "
        f'"{prompt.name}": ({prompt.version}, "{prompt.digest}") in this file.'
    )


@pytest.mark.parametrize("prompt", EVERY, ids=lambda prompt: prompt.name)
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


# --- the contact form's prompts, which came from another workflow ---------------------

CONTACT_EXPORT = N8N_EXPORT.with_name("Portfolio _ Contact Form -_ Email.json")

# n8n's lines that the contact form's prompts do not keep, each for a reason the
# prompt's own frontmatter gives. Every other line of n8n's has to be there still, in
# order. `contact_context` is not here: it was written for this project.
CONTACT_DROPPED: dict[str, tuple[str, ...]] = {
    # The workflow asked the classifier for it and never read the answer.
    "contact_classifier": (
        "- should_notify_me should be true for all legitimate messages, even vague ones",
        '  "should_notify_me": boolean,',
    ),
    # The reply is paragraphs of plain text now, so the sentence lost a word.
    "contact_reply": (
        "Your task is to write a short, professional, human-sounding HTML email reply to "
        "people who submit the contact form on mihaylov.io.",
    ),
    # The lines about Mihail, in role labels the knowledge base no longer gives him.
    # One block, contact_context.md, stands where they stood.
    "contact_brief_project": (
        "- Full-stack developer",
        "- Works with PHP, Laravel, CodeIgniter, Node.js, React",
        "- Builds automation systems and AI workflows",
        "- Works on web platforms, integrations, and custom systems",
    ),
    "contact_brief_collaboration": (
        "- Full-stack developer and automation engineer",
        "- Works on web development, automation, AI workflows, and digital products",
        "- Open to partnerships and collaboration on interesting projects",
    ),
    "contact_brief_job": (
        "- Full-stack developer",
        "- Works with PHP, Laravel, CodeIgniter, Node.js, React, APIs, automation",
        "- Experience with building platforms, integrations, and automation systems",
    ),
    "contact_brief_consultation": (
        "- Automation engineer and full-stack developer",
        "- Helps businesses with automation, integrations, system architecture, and AI workflows",
    ),
    "contact_brief_other": (),
}

# Where n8n's reply prompt turns to how the HTML is to be written. Everything from
# there on was rewritten as "Format rules", for paragraphs of plain text.
HTML_RULES = "HTML structure rules:"

# An n8n expression for one of the form's fields. Each became a named field of the
# brief, filled in by `str.format`: `{email}`, `{subject}`, `{message}`.
_FIELD_EXPRESSION = re.compile(r"\{\{ \$\('IF \| Is spam rule\?'\)\.item\.json\.body\.(\w+) \}\}")


def _contact_originals() -> dict[str, list[str]]:
    """n8n's own lines for each contact prompt, with the three differences that are not
    wording taken out: the ``=`` that marks an n8n expression, a field's expression in
    place of the field's name, and spaces at the ends of lines."""
    nodes = {
        node["name"]: node["parameters"]
        for node in json.loads(CONTACT_EXPORT.read_text(encoding="utf-8"))["nodes"]
    }
    originals = {
        "contact_classifier": nodes["AI Spam Classifier?"]["options"]["systemMessage"],
        "contact_reply": nodes["AI | Reply Generator"]["options"]["systemMessage"].partition(
            HTML_RULES
        )[0],
    }
    for reason in ("Project", "Collaboration", "Job", "Consultation", "Other"):
        assignments = nodes[f"SET | User Message - {reason}"]["assignments"]["assignments"]
        originals[f"contact_brief_{reason.lower()}"] = assignments[0]["value"]
    return {
        name: [
            _FIELD_EXPRESSION.sub(r"{\1}", line).rstrip()
            for line in text.removeprefix("=").strip().splitlines()
        ]
        for name, text in originals.items()
    }


@pytest.mark.skipif(not CONTACT_EXPORT.exists(), reason="the n8n export is only on the dev machine")
@pytest.mark.parametrize("name", sorted(CONTACT_DROPPED))
def test_a_contact_prompt_keeps_n8ns_lines_in_order_but_for_the_ones_listed(name: str) -> None:
    """ "n8n's prompts, word for word, with these changes" is what docs/CONTACT.md says
    of them. This is that sentence as a test: what a prompt adds is its own business,
    and what it no longer says has to be on the list above."""
    original = _contact_originals()[name]
    dropped = CONTACT_DROPPED[name]
    [prompt] = [prompt for prompt in EVERY if prompt.name == name]
    kept = prompt.text.splitlines()

    # The list cannot go stale: each line on it was n8n's, and is no longer ours.
    for line in dropped:
        assert line in original, line
        assert line not in kept, line

    ours = iter(kept)
    for line in original:
        if line in dropped:
            continue
        assert line in ours, line
