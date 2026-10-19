"""Transaction-neutral persistence operations for knowledge ingestion."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow


class DocumentVersionConflictError(ValueError):
    """Raised when an existing document version is staged with different data."""


def _chunk_business_values(chunk: KnowledgeChunk) -> tuple[object, ...]:
    return (
        chunk.chunk_id,
        chunk.document_type.value,
        chunk.title,
        chunk.section_path,
        chunk.content,
        chunk.model_id,
        chunk.source_uri,
        chunk.page_start,
        chunk.page_end,
    )


def _row_business_values(row: KnowledgeChunkRow) -> tuple[object, ...]:
    return (
        row.chunk_id,
        row.document_type,
        row.title,
        row.section_path,
        row.content,
        row.model_id,
        row.source_uri,
        row.page_start,
        row.page_end,
    )


async def stage_chunks(
    session: AsyncSession,
    chunks: Sequence[KnowledgeChunk],
) -> int:
    """Stage one immutable document version without committing its transaction.

    Returns the number of newly staged rows. Re-staging an identical version is
    a no-op; any business-field difference requires a new document version.
    """

    if not chunks:
        raise ValueError("at least one knowledge chunk is required")

    document_keys = {
        (chunk.document_id, chunk.document_version) for chunk in chunks
    }
    if len(document_keys) != 1:
        raise ValueError("all chunks must belong to one document version")
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("chunk_id values must be unique within a document version")

    document_id, document_version = next(iter(document_keys))
    # Serialize all writers of one document version before reading its chunks.
    # This lock belongs to the caller's transaction and releases on commit/rollback.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"riskmind:knowledge:{document_id}:{document_version}"},
    )
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.document_id == document_id,
            KnowledgeChunkRow.document_version == document_version,
        )
        .order_by(KnowledgeChunkRow.chunk_id)
    )
    existing = list(result.scalars())
    if existing:
        incoming_values = sorted(_chunk_business_values(chunk) for chunk in chunks)
        existing_values = sorted(_row_business_values(row) for row in existing)
        if incoming_values == existing_values:
            return 0
        raise DocumentVersionConflictError(
            f"document version {document_id}/{document_version} is immutable; "
            "use a new document_version"
        )

    session.add_all(
        [
            KnowledgeChunkRow(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_version=chunk.document_version,
                document_type=chunk.document_type.value,
                title=chunk.title,
                section_path=chunk.section_path,
                content=chunk.content,
                model_id=chunk.model_id,
                source_uri=chunk.source_uri,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                ingestion_status=IngestionStatus.PENDING.value,
            )
            for chunk in chunks
        ]
    )
    await session.flush()
    return len(chunks)


async def list_pending_chunks(
    session: AsyncSession,
    *,
    limit: int,
) -> list[KnowledgeChunkRow]:
    """Return one deterministic batch of chunks awaiting ingestion."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.ingestion_status
            == IngestionStatus.PENDING.value
        )
        .order_by(KnowledgeChunkRow.chunk_id)
        .limit(limit)
    )
    return list(result.scalars())


async def mark_chunks_ready(
    session: AsyncSession,
    chunk_ids: Sequence[str],
) -> int:
    """Mark a completed batch READY without committing its transaction."""

    unique_ids = set(chunk_ids)
    if not unique_ids:
        return 0
    result = await session.execute(
        update(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.chunk_id.in_(unique_ids),
            KnowledgeChunkRow.ingestion_status
            == IngestionStatus.PENDING.value,
        )
        .values(
            ingestion_status=IngestionStatus.READY.value,
            ready_at=func.now(),
            updated_at=func.now(),
        )
    )
    return int(result.rowcount or 0)


async def count_by_status(
    session: AsyncSession,
    status: IngestionStatus,
) -> int:
    """Count chunks in one ingestion state."""

    result = await session.execute(
        select(func.count())
        .select_from(KnowledgeChunkRow)
        .where(KnowledgeChunkRow.ingestion_status == status.value)
    )
    return int(result.scalar_one())
