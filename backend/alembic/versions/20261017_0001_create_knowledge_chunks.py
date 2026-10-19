"""create knowledge_chunks

Revision ID: 20261017_0001
Revises:
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261017_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=128), nullable=False),
        sa.Column("document_version", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("section_path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=True),
        sa.Column("source_uri", sa.String(length=2048), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column(
            "ingestion_status",
            sa.String(length=16),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "ingestion_status IN ('PENDING', 'READY')",
            name="ck_knowledge_chunks_ingestion_status",
        ),
        sa.CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start)",
            name="ck_knowledge_chunks_page_range",
        ),
        sa.PrimaryKeyConstraint("chunk_id"),
    )
    op.create_index(
        "ix_knowledge_chunks_document_version",
        "knowledge_chunks",
        ["document_id", "document_version"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_chunks_status_chunk_id",
        "knowledge_chunks",
        ["ingestion_status", "chunk_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_chunks_status_chunk_id",
        table_name="knowledge_chunks",
    )
    op.drop_index(
        "ix_knowledge_chunks_document_version",
        table_name="knowledge_chunks",
    )
    op.drop_table("knowledge_chunks")
