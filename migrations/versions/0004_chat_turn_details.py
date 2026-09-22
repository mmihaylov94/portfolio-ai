"""Link an answer to its question, and record how it was produced

Revision ID: 0004
Revises: 0003
Created: step 3, the assistant core

Three columns that migration 0002 could not have known it needed, because nothing
had answered a question yet. Each exists because the value cannot be reconstructed
later -- the same reason ``top_score`` and ``fallback_used`` landed early.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        alter table chat_messages
            add column reply_to_id   bigint references chat_messages(id) on delete cascade,
            add column llm_calls     jsonb,
            add column first_token_ms int
    """)

    # reply_to_id points from an answer to the question it answers. Without it the
    # pairing is "the nearest user row above this one in the same session", which is
    # a correct-looking query that goes wrong the first time two messages arrive at
    # once. Deliverable 4 needs it constantly: a thumbs-down arrives against an
    # answer, and every useful thing to do with it starts from the question.
    #
    # llm_calls holds one record per OpenAI call behind the answer -- step, model,
    # prompt version, tokens (cached and reasoning separately), latency, cost. The
    # columns beside it hold the totals, which answer "what did this cost"; this
    # answers "where did it go", and that is what says whether the classifier is
    # worth its latency or a prompt version changed the bill.
    #
    # first_token_ms is how long the visitor waited before words appeared, as
    # opposed to latency_ms, which is how long the whole answer took. With streaming
    # they are very different numbers, and only the first one is what the chat feels
    # like.

    op.execute("create index on chat_messages (reply_to_id)")


def downgrade() -> None:
    op.execute("""
        alter table chat_messages
            drop column reply_to_id,
            drop column llm_calls,
            drop column first_token_ms
    """)
