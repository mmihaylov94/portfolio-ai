"""Knowledge base: documents, chunks, and the vector index

Revision ID: 0001
Revises:
Created: lesson 7

The knowledge base half of ARCHITECTURE.md section 5. A document is one Markdown
file from the source repository; a chunk is one section of it, with the embedding
retrieval actually searches.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Installed into public deliberately. Extensions are shared across the
    # database, and putting this in our own schema would mean every connection
    # needed our schema on its search path merely to understand what a vector is.
    op.execute("create extension if not exists vector with schema public")

    op.execute("""
        create table documents (
            id            bigserial primary key,
            doc_id        text not null unique,
            source_type   text not null default 'knowledgebase',
            page_type     text,
            title         text not null,
            url           text,
            tags          text[] not null default '{}',
            last_verified date,
            source_file   text,
            source_path   text not null,
            git_blob_sha  text,
            content_hash  text not null,
            indexed_at    timestamptz not null,
            created_at    timestamptz not null default now(),
            updated_at    timestamptz not null default now()
        )
    """)

    # doc_id is the stable identity from the file's frontmatter, which is what
    # ingestion upserts against. content_hash is how a re-run knows a document is
    # unchanged and can skip paying for embeddings again.

    op.execute("""
        create table chunks (
            id              bigserial primary key,
            document_id     bigint not null references documents(id) on delete cascade,
            chunk_index     int not null,
            section         text,
            section_title   text,
            content         text not null,
            token_count     int,
            embedding       vector(1536) not null,
            embedding_model text not null,
            created_at      timestamptz not null default now(),
            unique (document_id, chunk_index)
        )
    """)

    # on delete cascade: chunks have no meaning without their document, so
    # removing a document should take its chunks with it rather than leaving rows
    # pointing at nothing.

    op.execute("create index on chunks (document_id)")

    # The index retrieval depends on. HNSW builds a navigable graph over the
    # vectors, so a similarity search reads a fraction of the table instead of
    # scoring every row. vector_cosine_ops has to match the distance operator the
    # query uses -- ARCHITECTURE.md section 8 orders by <=>, which is cosine, and
    # an index built for a different operator is simply ignored.
    op.execute("create index on chunks using hnsw (embedding vector_cosine_ops)")


def downgrade() -> None:
    # Reverse order: chunks reference documents, so the child goes first.
    op.drop_table("chunks")
    op.drop_table("documents")

    # The extension is left alone on purpose. It is shared with everything else in
    # the database -- including, on the production server, n8n's own vector tables
    # -- so dropping it here would reach outside this project's own schema.
