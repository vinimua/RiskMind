"""Profile-isolated knowledge builds and context persistence.

Revision ID: 20261024_0002
Revises: 20261017_0001
"""

from alembic import op
import sqlalchemy as sa


revision = "20261024_0002"
down_revision = "20261017_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing chunk IDs, statuses, content and provenance are untouched.
    op.add_column("knowledge_chunks", sa.Column(
        "index_profile", sa.String(64), server_default="baseline-v1", nullable=False,
    ))
    op.drop_constraint("knowledge_chunks_pkey", "knowledge_chunks", type_="primary")
    op.create_primary_key("knowledge_chunks_pkey", "knowledge_chunks", ["chunk_id", "index_profile"])
    op.create_check_constraint("ck_knowledge_chunks_profile", "knowledge_chunks",
                               "index_profile IN ('baseline-v1', 'parent-child-v1')")
    for name, columns in (
        ("ix_knowledge_chunks_document_version", ["index_profile", "document_id", "document_version"]),
        ("ix_knowledge_chunks_status_chunk_id", ["index_profile", "ingestion_status", "chunk_id"]),
    ):
        op.drop_index(name, table_name="knowledge_chunks")
        op.create_index(name, "knowledge_chunks", columns)
    op.create_table(
        "knowledge_documents",
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("document_version", sa.String(64), primary_key=True),
        sa.Column("source_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "knowledge_builds",
        sa.Column("index_profile", sa.String(64), primary_key=True),
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("document_version", sa.String(64), primary_key=True),
        sa.Column("prepared_digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["document_id", "document_version"],
                                ["knowledge_documents.document_id", "knowledge_documents.document_version"]),
        sa.CheckConstraint("index_profile IN ('baseline-v1', 'parent-child-v1')", name="ck_knowledge_builds_profile"),
    )
    op.create_table(
        "knowledge_contexts",
        sa.Column("index_profile", sa.String(64), primary_key=True),
        sa.Column("context_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("document_version", sa.String(64), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("section_path", sa.String(1024), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_id", sa.String(128)),
        sa.Column("source_uri", sa.String(2048), nullable=False),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.ForeignKeyConstraint(["index_profile", "document_id", "document_version"],
                                ["knowledge_builds.index_profile", "knowledge_builds.document_id", "knowledge_builds.document_version"]),
        sa.CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start > 0 AND page_end >= page_start AND page_start IS NOT NULL AND page_end IS NOT NULL)",
            name="ck_knowledge_contexts_pages",
        ),
    )
    op.create_table(
        "knowledge_context_links",
        sa.Column("index_profile", sa.String(64), primary_key=True),
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("context_id", sa.String(128), nullable=False),
        sa.Column("child_ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id", "index_profile"], ["knowledge_chunks.chunk_id", "knowledge_chunks.index_profile"]),
        sa.ForeignKeyConstraint(["index_profile", "context_id"], ["knowledge_contexts.index_profile", "knowledge_contexts.context_id"]),
        sa.UniqueConstraint("index_profile", "context_id", "child_ordinal"),
        sa.CheckConstraint("child_ordinal >= 0", name="ck_knowledge_context_links_ordinal"),
    )
    op.create_table(
        "knowledge_collections",
        sa.Column("collection", sa.String(255), primary_key=True),
        sa.Column("index_profile", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.CheckConstraint("purpose IN ('INGESTION', 'REBUILD')", name="ck_knowledge_collections_purpose"),
        sa.CheckConstraint("index_profile IN ('baseline-v1', 'parent-child-v1')", name="ck_knowledge_collections_profile"),
    )
    op.create_index(
        "uq_knowledge_collections_ingestion_profile", "knowledge_collections", ["index_profile"],
        unique=True, postgresql_where=sa.text("purpose = 'INGESTION'"),
    )


def downgrade() -> None:
    # Refuse to silently discard enrolled sources, builds or collection ownership.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM knowledge_documents)
           OR EXISTS (SELECT 1 FROM knowledge_collections)
           OR EXISTS (SELECT 1 FROM knowledge_chunks WHERE index_profile <> 'baseline-v1')
        THEN RAISE EXCEPTION 'cannot downgrade populated index profiles; export and retire them explicitly';
        END IF;
    END $$""")
    for table in ("knowledge_context_links", "knowledge_contexts", "knowledge_builds",
                  "knowledge_documents", "knowledge_collections"):
        op.drop_table(table)
    op.drop_constraint("ck_knowledge_chunks_profile", "knowledge_chunks", type_="check")
    op.drop_constraint("knowledge_chunks_pkey", "knowledge_chunks", type_="primary")
    op.create_primary_key("knowledge_chunks_pkey", "knowledge_chunks", ["chunk_id"])
    for name, columns in (
        ("ix_knowledge_chunks_document_version", ["document_id", "document_version"]),
        ("ix_knowledge_chunks_status_chunk_id", ["ingestion_status", "chunk_id"]),
    ):
        op.drop_index(name, table_name="knowledge_chunks")
        op.create_index(name, "knowledge_chunks", columns)
    op.drop_column("knowledge_chunks", "index_profile")
