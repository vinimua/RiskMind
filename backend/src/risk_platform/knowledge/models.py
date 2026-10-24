"""PostgreSQL persistence model for authoritative knowledge chunks."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column

from risk_platform.db.database import Base


class IngestionStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_chunks_profile",
        ),
        CheckConstraint(
            "ingestion_status IN ('PENDING', 'READY')",
            name="ck_knowledge_chunks_ingestion_status",
        ),
        CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start)",
            name="ck_knowledge_chunks_page_range",
        ),
        Index(
            "ix_knowledge_chunks_status_chunk_id",
            "index_profile",
            "ingestion_status",
            "chunk_id",
        ),
        Index(
            "ix_knowledge_chunks_document_version",
            "index_profile",
            "document_id",
            "document_version",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    section_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingestion_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=IngestionStatus.PENDING.value,
        server_default=IngestionStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    index_profile: Mapped[str] = mapped_column(
        String(64), primary_key=True, default="baseline-v1", server_default="baseline-v1",
    )


class KnowledgeDocumentRow(Base):
    """Canonical source identity shared across profiles; legacy sources enroll on use."""

    __tablename__ = "knowledge_documents"
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_json: Mapped[str] = mapped_column(Text, nullable=False)


class KnowledgeBuildRow(Base):
    """Immutable prepared-artifact digest, including order, for retry comparison."""

    __tablename__ = "knowledge_builds"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["knowledge_documents.document_id", "knowledge_documents.document_version"],
        ),
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_builds_profile",
        ),
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    prepared_digest: Mapped[str] = mapped_column(String(64), nullable=False)


class KnowledgeContextRow(Base):
    __tablename__ = "knowledge_contexts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["index_profile", "document_id", "document_version"],
            ["knowledge_builds.index_profile", "knowledge_builds.document_id",
             "knowledge_builds.document_version"],
        ),
        CheckConstraint(
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start > 0 AND page_end >= page_start "
            "AND page_start IS NOT NULL AND page_end IS NOT NULL)",
            name="ck_knowledge_contexts_pages",
        ),
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    section_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(128))
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)


class KnowledgeContextLinkRow(Base):
    __tablename__ = "knowledge_context_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id", "index_profile"],
            ["knowledge_chunks.chunk_id", "knowledge_chunks.index_profile"],
        ),
        ForeignKeyConstraint(
            ["index_profile", "context_id"],
            ["knowledge_contexts.index_profile", "knowledge_contexts.context_id"],
        ),
        UniqueConstraint("index_profile", "context_id", "child_ordinal"),
        CheckConstraint("child_ordinal >= 0", name="ck_knowledge_context_links_ordinal"),
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    context_id: Mapped[str] = mapped_column(String(128), nullable=False)
    child_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class KnowledgeCollectionRow(Base):
    """Immutable ownership of a physical collection and its write purpose."""

    __tablename__ = "knowledge_collections"
    __table_args__ = (
        CheckConstraint("purpose IN ('INGESTION', 'REBUILD')", name="ck_knowledge_collections_purpose"),
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_collections_profile",
        ),
        Index(
            "uq_knowledge_collections_ingestion_profile", "index_profile", unique=True,
            postgresql_where=text("purpose = 'INGESTION'"),
            sqlite_where=text("purpose = 'INGESTION'"),
        ),
    )
    collection: Mapped[str] = mapped_column(String(255), primary_key=True)
    index_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
