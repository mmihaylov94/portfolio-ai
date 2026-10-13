"""An eval case no longer points at the message it came from

Revision ID: 0007
Revises: 0006
Created: step 7, the analytics

Migration 0003 gave ``eval_cases`` a ``source_message_id``: the visitor's question a
case was promoted from. It was to be "the join that closes the loop". Nothing could
ever fill it. Eval cases are stored where evals run, which is the development
database; visitors' messages are in production's. The two ids never share a
database.

So a promoted case is printed instead, as YAML that goes into a dataset file
(``analytics promote-case``), and it carries where it came from in its ``notes``. The
column is null in every row everywhere, and goes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The foreign key constraint goes with the column. Dropping a column drops
    # every index and table constraint the column is part of, one that spans other
    # columns too, so it is worth knowing what a column is in before it goes. This
    # one is in its own foreign key and nothing else.
    op.execute("alter table eval_cases drop column source_message_id")


def downgrade() -> None:
    # As 0003 declared it. `on delete set null`, because a case was to outlive the
    # message the retention sweep would one day delete.
    op.execute("""
        alter table eval_cases
            add column source_message_id bigint references chat_messages(id) on delete set null
    """)
