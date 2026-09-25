"""What the eval harness needs that the eval tables were designed without

Revision ID: 0005
Revises: 0004
Created: step 5, the evals

Migration 0003 laid the eval tables out in lesson 7, before the assistant existed.
Building the harness against the real assistant turned up what they lack. Nothing
has ever written to these tables, in any environment, so every change here lands
on empty tables.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("alter table eval_datasets add column content_hash text")

    # content_hash is what freezes a dataset. Scores are only comparable across runs
    # of identical cases, so once a dataset has a complete run, a YAML file whose hash
    # no longer matches is refused rather than quietly re-seeded under the same name.

    op.execute("""
        alter table eval_cases
            add column key              text,
            add column history          jsonb not null default '[]',
            add column expect_fallback  boolean not null default false,
            add column also_accept      text[] not null default '{}'
                check (also_accept <@ array['mihail_related', 'small_talk', 'out_of_scope'])
    """)
    # The tables are empty, but a migration that assumes so fails badly the one time
    # it is wrong. Any row that existed would get its id as a key.
    op.execute("update eval_cases set key = id::text where key is null")
    op.execute("alter table eval_cases alter column key set not null")
    op.execute("""
        alter table eval_cases
            add constraint eval_cases_dataset_id_key_key unique (dataset_id, key)
    """)

    # key names a case in reports ("threadline-stack regressed") instead of an id
    # that changes every time a dataset is re-seeded before its first run.
    #
    # history is the conversation before the question, for follow-ups like "yes
    # please": the case the classifier's previous-exchange context exists for.
    #
    # expect_fallback marks a question the knowledge base cannot answer, where the
    # right answer is to say so.
    #
    # also_accept lists routes that are right too. "Are you Mihail?" is answered
    # properly by the small-talk route and by the knowledge-base one; counting either
    # as a misroute would be noise in the classification score.

    op.execute("alter table eval_runs add constraint eval_runs_label_key unique (label)")

    # Runs are compared by label (compare baseline effort-low), so a label has to
    # name exactly one run.

    op.execute("""
        alter table eval_results
            add column first_token_ms int,
            add column top_score      real,
            add column fallback_used  boolean,
            add column links_removed  int,
            add column searches       jsonb,
            add column calls          jsonb,
            add column judge_cost_usd numeric(10, 6),
            add column error          text
    """)

    # first_token_ms is the number the reasoning-effort decision turns on: how long a
    # visitor waits before words appear. The rest are the assistant's own signals,
    # recorded the way chat_messages records them. calls holds one record per OpenAI
    # call, the judge's included -- model, tokens with reasoning apart, latency, cost --
    # as chat_messages.llm_calls does, and reasoning tokens by effort level are much of
    # what an effort comparison is about. judge_cost_usd keeps grading apart from
    # cost_usd, which is what the answer itself cost. error holds why a case produced
    # no answer, or no grade, without stopping the run.


def downgrade() -> None:
    op.execute("""
        alter table eval_results
            drop column first_token_ms,
            drop column top_score,
            drop column fallback_used,
            drop column links_removed,
            drop column searches,
            drop column calls,
            drop column judge_cost_usd,
            drop column error
    """)
    op.execute("alter table eval_runs drop constraint eval_runs_label_key")
    op.execute("alter table eval_cases drop constraint eval_cases_dataset_id_key_key")
    op.execute("""
        alter table eval_cases
            drop column key,
            drop column history,
            drop column expect_fallback,
            drop column also_accept
    """)
    op.execute("alter table eval_datasets drop column content_hash")
