"""Eval datasets, cases, runs and results

Revision ID: 0003
Revises: 0002
Created: lesson 7

The eval harness from ARCHITECTURE.md section 9. A dataset holds cases; a run
applies one configuration to one dataset; a result is one case under one run.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        create table eval_datasets (
            id          bigserial primary key,
            name        text not null unique,
            description text,
            created_at  timestamptz not null default now()
        )
    """)

    op.execute("""
        create table eval_cases (
            id                bigserial primary key,
            dataset_id        bigint not null references eval_datasets(id) on delete cascade,
            question          text not null,
            category          text not null
                              check (category in ('mihail_related', 'small_talk', 'out_of_scope')),
            expected_doc_ids  text[] not null default '{}',
            reference_answer  text,
            must_include      text[] not null default '{}',
            must_not_include  text[] not null default '{}',
            source_message_id bigint references chat_messages(id) on delete set null,
            notes             text,
            created_at        timestamptz not null default now()
        )
    """)

    # source_message_id is the join that closes the loop from ARCHITECTURE.md
    # section 10: a real question that retrieved badly becomes a permanent
    # regression test once the missing content is written.
    #
    # on delete set null rather than cascade -- the ninety-day retention sweep will
    # eventually remove the original message, and the eval case must outlive it.
    # Cascade here would quietly delete the test.

    op.execute("""
        create table eval_runs (
            id          bigserial primary key,
            dataset_id  bigint not null references eval_datasets(id) on delete cascade,
            label       text not null,
            config      jsonb not null,
            status      text not null default 'running'
                        check (status in ('running', 'complete', 'failed')),
            started_at  timestamptz not null default now(),
            finished_at timestamptz,
            totals      jsonb
        )
    """)

    # config holds the whole configuration the run used -- models, top_k, prompt
    # versions, git sha. It is jsonb rather than columns because the set of things
    # worth recording will change, and a schema migration per knob would be
    # tiresome. Results months apart stay comparable because the configuration
    # travels with them.

    op.execute("""
        create table eval_results (
            id                  bigserial primary key,
            run_id              bigint not null references eval_runs(id) on delete cascade,
            case_id             bigint not null references eval_cases(id) on delete cascade,
            answer              text,
            classification      text,
            retrieved_chunk_ids bigint[],
            retrieved_doc_ids   text[],
            recall_at_k         real,
            precision_at_k      real,
            mrr                 real,
            judge_scores        jsonb,
            judge_rationale     text,
            rule_violations     jsonb,
            prompt_tokens       int,
            completion_tokens   int,
            cost_usd            numeric(10, 6),
            latency_ms          int,
            created_at          timestamptz not null default now(),
            unique (run_id, case_id)
        )
    """)

    op.execute("create index on eval_results (run_id)")


def downgrade() -> None:
    # Order follows the dependency graph, children first. eval_results points at
    # both eval_runs and eval_cases so it has to go first; those two know nothing
    # about each other, so their order between themselves is free; eval_datasets
    # goes last because everything pointed at it.
    #
    # The constraint reaches beyond this file. eval_cases references chat_messages
    # from 0002, so these tables must be gone before 0002's downgrade can drop it
    # -- which is what makes `alembic downgrade base` work rather than failing two
    # steps in.
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
    op.drop_table("eval_datasets")
