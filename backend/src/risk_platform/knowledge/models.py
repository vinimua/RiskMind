"""PostgreSQL persistence model for authoritative knowledge chunks."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from risk_platform.db.database import Base


class IngestionStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"


class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
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
            "ingestion_status",
            "chunk_id",
        ),
        Index(
            "ix_knowledge_chunks_document_version",
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
