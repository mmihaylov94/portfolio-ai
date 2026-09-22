"""Chat history, feedback, and the content gaps derived from them

Revision ID: 0002
Revises: 0001
Created: lesson 7

Everything the assistant records while answering, plus the analytics tables built
from it. ARCHITECTURE.md sections 5 and 10.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        create table chat_sessions (
            id             bigserial primary key,
            session_id     text not null unique,
            created_at     timestamptz not null default now(),
            last_seen_at   timestamptz not null default now(),
            client_ip_hash text,
            user_agent     text,
            referrer       text
        )
    """)

    # client_ip_hash, not client_ip. Hashed with a salt before it ever reaches the
    # database, per the privacy decision in ARCHITECTURE.md section 10. The column
    # name is part of that: it makes storing a raw address look wrong.

    op.execute("""
        create table chat_messages (
            id                  bigserial primary key,
            session_id          bigint not null references chat_sessions(id) on delete cascade,
            role                text not null check (role in ('user', 'assistant')),
            content             text not null,
            tool_calls          jsonb,
            retrieved_chunk_ids bigint[],
            classification      text,
            top_score           real,
            fallback_used       boolean not null default false,
            model               text,
            prompt_tokens       int,
            completion_tokens   int,
            cost_usd            numeric(10, 6),
            latency_ms          int,
            created_at          timestamptz not null default now()
        )
    """)

    # top_score, fallback_used and classification exist for the analytics in
    # deliverable 4. They are recorded at answer time because they cannot be
    # reconstructed afterwards -- which is why they land now, months before
    # anything reads them.
    #
    # cost_usd is numeric rather than a float. Money in binary floating point
    # accumulates error, and this column gets summed.

    op.execute("create index on chat_messages (session_id, created_at)")
    op.execute("create index on chat_messages (created_at)")

    op.execute("""
        create table message_feedback (
            id         bigserial primary key,
            message_id bigint not null unique references chat_messages(id) on delete cascade,
            rating     smallint not null check (rating in (-1, 1)),
            comment    text,
            created_at timestamptz not null default now()
        )
    """)

    # unique on message_id: one verdict per answer. Voting again updates the row
    # rather than adding a second opinion.

    op.execute("""
        create table content_gaps (
            id                bigserial primary key,
            cluster_label     text not null,
            centroid          vector(1536) not null,
            question_count    int not null,
            example_questions text[] not null,
            avg_top_score     real,
            negative_rate     real,
            status            text not null default 'open'
                              check (status in ('open', 'written', 'wont_fix')),
            resolved_doc_id   text,
            first_seen_at     timestamptz not null,
            last_seen_at      timestamptz not null
        )
    """)

    # The table that turns analytics into a writing queue: each row is "people
    # keep asking about this and the answers are poor". Closing one means writing
    # a knowledge base article.


def downgrade() -> None:
    # Children before parents: message_feedback references chat_messages, which
    # references chat_sessions.
    op.drop_table("content_gaps")
    op.drop_table("message_feedback")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
