"""Content gaps keep the ids of their questions, not the questions

Revision ID: 0006
Revises: 0005
Created: step 7, the analytics

Migration 0002 laid ``content_gaps`` out before anything could fill it. Building
what fills it turned up one column that must not exist and four that were missing.
Nothing has ever written to the table, in any environment, so every change here
lands on an empty table.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        alter table content_gaps
            drop column example_questions,
            add column example_message_ids bigint[] not null default '{}',
            add column fallback_count      int not null default 0,
            add column refreshed_at        timestamptz,
            add column resolved_at         timestamptz
    """)

    # example_questions was text: a copy of what visitors typed, in a table that is
    # kept indefinitely. Chat is deleted after 90 days, and a question quoted here
    # would have outlived that. So the gap keeps the ids of a few of its questions
    # instead. An id says nothing by itself, and resolves to words only for as long
    # as the message it names still exists.
    #
    # They are not a foreign key, and cannot be: Postgres has no foreign keys from
    # the elements of an array. An id whose message the purge has taken simply finds
    # nothing, and the next refresh replaces the list.
    #
    # fallback_count is how many of the gap's questions were answered with "I do
    # not have that information", beside question_count, which also counts the ones
    # answered with nothing close to answer from.
    #
    # refreshed_at is when the counts were last worked out. resolved_at is when the
    # gap was marked written or wont_fix: a question in it that failed after that
    # date says the article did not close it.


def downgrade() -> None:
    op.execute("""
        alter table content_gaps
            drop column example_message_ids,
            drop column fallback_count,
            drop column refreshed_at,
            drop column resolved_at,
            add column example_questions text[] not null default '{}'
    """)

    # The default is there for the rows the table holds by now: a column that is
    # `not null` cannot be added to them without a value to give each. Migration
    # 0002 declared the column with no default, so it is taken off again, and what
    # is left is the table 0005 knew.
    op.execute("alter table content_gaps alter column example_questions drop default")
