"""Transaction-neutral persistence operations for knowledge ingestion."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.chunking import KnowledgeDocument, chunk_document
from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy, PreparedKnowledge
from risk_platform.knowledge.models import (
    KnowledgeBuildRow, KnowledgeCollectionRow, KnowledgeContextLinkRow,
    KnowledgeContextRow, KnowledgeDocumentRow,
)


class DocumentVersionConflictError(ValueError):
    """Raised when an existing document version is staged with different data."""


async def lock_profile_source(
    session: AsyncSession, profile: IndexProfile, *, shared: bool = False,
) -> None:
    """Writers share this lock; serving acceptance/activation take it exclusively."""
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(
        text(f"SELECT {function}(hashtextextended(:key, 0))"),
        {"key": f"riskmind:knowledge-source:{profile.value}"},
    )


def _canonical(value: BaseModel) -> str:
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def stage_prepared(
    session: AsyncSession,
    document: KnowledgeDocument,
    prepared: PreparedKnowledge,
    *,
    index_profile: IndexProfile,
) -> int:
    """Atomically stage source, children, contexts and links; caller owns commit.

    The source lock is shared with legacy staging and spans all index profiles.
    A profile denotes frozen build semantics, so mismatched artifacts fail closed.
    """
    profile = IndexProfile(index_profile)
    document = KnowledgeDocument.model_validate_json(document.model_dump_json())
    prepared = PreparedKnowledge.model_validate_json(prepared.model_dump_json())
    expected = (
        BaselineIndexBuildStrategy().build(document) if profile == IndexProfile.BASELINE
        else ParentChildIndexBuildStrategy().build(document)
    )
    if prepared != expected:
        raise ValueError("prepared knowledge does not match the frozen index profile")
    if not prepared.search_chunks:
        raise ValueError("at least one knowledge chunk is required")
    key = (document.document_id, document.document_version)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"riskmind:knowledge:{key[0]}:{key[1]}"},
    )
    source_json = _canonical(document)
    source = await session.get(KnowledgeDocumentRow, key)
    if source is not None and source.source_json != source_json:
        raise DocumentVersionConflictError("document version is immutable across index profiles")
    if source is None:
        # Existing Stage 5 rows have no original document. Enroll only if the
        # supplied source reproduces every legacy authoritative business field.
        legacy = list((await session.execute(select(KnowledgeChunkRow).where(
            KnowledgeChunkRow.document_id == key[0],
            KnowledgeChunkRow.document_version == key[1],
            KnowledgeChunkRow.index_profile == IndexProfile.BASELINE.value,
        ))).scalars())
        if legacy and sorted(_row_business_values(row) for row in legacy) != sorted(
            _chunk_business_values(chunk) for chunk in chunk_document(document)
        ):
            raise DocumentVersionConflictError("legacy document version is immutable")
        session.add(KnowledgeDocumentRow(
            document_id=key[0], document_version=key[1], source_json=source_json,
        ))
        await session.flush()
    digest = hashlib.sha256(_canonical(prepared).encode("utf-8")).hexdigest()
    existing = await session.get(KnowledgeBuildRow, (profile.value, *key))
    if existing is not None:
        if existing.prepared_digest != digest:
            raise DocumentVersionConflictError("index build is immutable; use a new profile or document version")
        return 0
    session.add(KnowledgeBuildRow(
        index_profile=profile.value, document_id=key[0], document_version=key[1],
        prepared_digest=digest,
    ))
    await session.flush()
    count = await _stage_chunks(session, prepared.search_chunks, index_profile=profile)
    session.add_all([
        KnowledgeContextRow(index_profile=profile.value, **unit.model_dump())
        for unit in prepared.context_units
    ])
    await session.flush()
    session.add_all([
        KnowledgeContextLinkRow(index_profile=profile.value, **link.model_dump())
        for link in prepared.context_links
    ])
    await session.flush()
    return count


async def bind_collection(
    session: AsyncSession, *, collection: str, index_profile: IndexProfile, purpose: str,
) -> None:
    """Bind ownership before external writes; one ingestion target per profile.

    Rebuild targets never become ingestion targets implicitly: READY refers only
    to the profile's ingestion target, not to arbitrary rebuilt collections.
    """
    profile = IndexProfile(index_profile)
    if purpose not in {"INGESTION", "REBUILD"}:
        raise ValueError("invalid collection purpose")
    # Same lock order in all writers; also serializes competing profile owners.
    for key in (f"profile:{profile.value}", f"collection:{collection}"):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"riskmind:knowledge-index:{key}"},
        )
    existing = await session.get(KnowledgeCollectionRow, collection)
    if existing is not None:
        if (existing.index_profile, existing.purpose) != (profile.value, purpose):
            raise ValueError("physical collection is already bound to another profile or purpose")
        return
    if purpose == "INGESTION":
        target = (await session.execute(select(KnowledgeCollectionRow).where(
            KnowledgeCollectionRow.index_profile == profile.value,
            KnowledgeCollectionRow.purpose == purpose,
        ))).scalar_one_or_none()
        if target is not None:
            raise ValueError("profile already has an ingestion target; use rebuild for a new collection")
    session.add(KnowledgeCollectionRow(
        collection=collection, index_profile=profile.value, purpose=purpose,
    ))
    await session.flush()


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
    """Compatibility entry point: stage Baseline chunks only."""
    if any(chunk.chunk_id.startswith("pc-child-") for chunk in chunks):
        raise ValueError("Parent-Child output must use stage_prepared with its index profile")
    return await _stage_chunks(session, chunks, index_profile=IndexProfile.BASELINE)


async def _stage_chunks(
    session: AsyncSession,
    chunks: Sequence[KnowledgeChunk],
    *,
    index_profile: IndexProfile,
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
    # Shared with serving validation/activation, which take the exclusive form.
    await lock_profile_source(session, index_profile, shared=True)
    if index_profile == IndexProfile.BASELINE:
        source = await session.get(KnowledgeDocumentRow, (document_id, document_version))
        if source is not None:
            expected = chunk_document(KnowledgeDocument.model_validate_json(source.source_json))
            if sorted(_chunk_business_values(c) for c in chunks) != sorted(
                _chunk_business_values(c) for c in expected
            ):
                raise DocumentVersionConflictError("document version is immutable across index profiles")
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.document_id == document_id,
            KnowledgeChunkRow.document_version == document_version,
            KnowledgeChunkRow.index_profile == index_profile.value,
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
                index_profile=index_profile.value,
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
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> list[KnowledgeChunkRow]:
    """Return one deterministic batch of chunks awaiting ingestion."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
            KnowledgeChunkRow.ingestion_status
            == IngestionStatus.PENDING.value
        )
        .order_by(KnowledgeChunkRow.chunk_id)
        .limit(limit)
    )
    return list(result.scalars())


async def list_rebuild_chunks(
    session: AsyncSession,
    *,
    limit: int,
    after_chunk_id: str | None = None,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> list[KnowledgeChunkRow]:
    """Page through all authoritative chunks without changing their status."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    statement = select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
        KnowledgeChunkRow.ingestion_status.in_(
            (IngestionStatus.PENDING.value, IngestionStatus.READY.value)
        )
    )
    if after_chunk_id is not None:
        statement = statement.where(KnowledgeChunkRow.chunk_id > after_chunk_id)
    result = await session.execute(
        statement.order_by(KnowledgeChunkRow.chunk_id).limit(limit).execution_options(populate_existing=True)
    )
    return list(result.scalars())


async def count_chunks(
    session: AsyncSession, *, index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """Count all authoritative chunks for rebuild source-change detection."""

    result = await session.execute(
        select(func.count()).select_from(KnowledgeChunkRow).where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
        )
    )
    return int(result.scalar_one())


async def mark_chunks_ready(
    session: AsyncSession,
    chunk_ids: Sequence[str],
    *,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """Mark a completed batch READY without committing its transaction."""

    unique_ids = set(chunk_ids)
    if not unique_ids:
        return 0
    result = await session.execute(
        update(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
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
    *,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """Count chunks in one ingestion state."""

    result = await session.execute(
        select(func.count())
        .select_from(KnowledgeChunkRow)
        .where(KnowledgeChunkRow.ingestion_status == status.value)
        .where(KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value)
    )
    return int(result.scalar_one())
