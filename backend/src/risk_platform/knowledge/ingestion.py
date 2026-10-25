"""Move pending knowledge chunks into the Milvus read model."""

from __future__ import annotations

from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.embeddings import EmbeddingClient, get_embedding_client
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.repository import (
    count_chunks,
    list_pending_chunks,
    list_rebuild_chunks,
    mark_chunks_ready,
    bind_collection,
)
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.serving import collection_write_guard, get_accepted_build, validate_serving_collection


DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 64


async def ingest_pending(
    *,
    collection: str,
    index_profile: IndexProfile = IndexProfile.BASELINE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None,
    milvus_client: MilvusClient | None = None,
) -> int:
    """Upsert pending chunks, then mark them ready after Milvus flush succeeds.

    Callers supply a physical collection. A failed batch remains PENDING and
    can be retried using the same stable chunk IDs.
    """

    _validate_options(collection, batch_size)

    profile = IndexProfile(index_profile)
    await _preflight_collection(collection, profile, milvus_client)
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        async with session.begin():
            await bind_collection(
                session, collection=collection, index_profile=profile, purpose="INGESTION",
            )
    completed = 0
    while True:
        async with sessions() as session:
            pending = await list_pending_chunks(session, limit=batch_size, index_profile=profile)
        if not pending:
            return completed

        async with collection_write_guard(sessions, collection):
            await _embed_and_upsert(
                pending,
                collection=collection,
                embedding_client=embedding_client,
                milvus_client=milvus_client,
                index_profile=profile,
            )

        async with sessions() as session:
            async with session.begin():
                completed += await mark_chunks_ready(
                    session, [row.chunk_id for row in pending], index_profile=profile,
                )


async def rebuild_collection(
    *,
    collection: str,
    index_profile: IndexProfile = IndexProfile.BASELINE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None,
    milvus_client: MilvusClient | None = None,
) -> int:
    """Re-embed PENDING and READY chunks into a physical collection.

    PostgreSQL statuses are read only. Use a new empty physical collection for
    a complete rebuild; rerunning an interrupted build safely upserts by ID.
    """

    _validate_options(collection, batch_size)
    profile = IndexProfile(index_profile)
    await _preflight_collection(collection, profile, milvus_client)
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        async with session.begin():
            await bind_collection(
                session, collection=collection, index_profile=profile, purpose="REBUILD",
            )
    async with sessions() as session:
        accepted = await get_accepted_build(session, collection)
    if accepted is not None:
        receipt = await validate_serving_collection(
            collection=collection, index_profile=profile, session_factory=sessions, milvus_client=milvus_client,
        )
        return receipt.source_chunk_count
    async with sessions() as session:
        expected_count = await count_chunks(session, index_profile=profile)
    after_chunk_id = None
    written = 0
    while True:
        async with sessions() as session:
            chunks = await list_rebuild_chunks(
                session, limit=batch_size, after_chunk_id=after_chunk_id, index_profile=profile,
            )
        if not chunks:
            if written == 0:
                def ensure_empty_collection() -> None:
                    client = milvus_client or milvus.get_client()
                    milvus.ensure_collection(client, collection, index_profile=profile)

                await milvus.acall(ensure_empty_collection)
            async with sessions() as session:
                actual_count = await count_chunks(session, index_profile=profile)
            if actual_count != expected_count:
                raise RuntimeError(
                    "authoritative knowledge changed during rebuild; rerun rebuild"
                )
            await validate_serving_collection(
                collection=collection, index_profile=profile,
                session_factory=sessions, milvus_client=milvus_client,
            )
            return written

        async with collection_write_guard(sessions, collection):
            await _embed_and_upsert(
                chunks,
                collection=collection,
                embedding_client=embedding_client,
                milvus_client=milvus_client,
                index_profile=profile,
            )
        written += len(chunks)
        after_chunk_id = chunks[-1].chunk_id


async def _preflight_collection(
    collection: str, profile: IndexProfile, client: MilvusClient | None,
) -> None:
    def check() -> None:
        milvus.validate_collection_for_profile(client or milvus.get_client(), collection, profile)

    await milvus.acall(check)


def _validate_options(collection: str, batch_size: int) -> None:
    if not collection or not collection.strip():
        raise ValueError("a physical collection name is required")
    if collection == milvus.ACTIVE_COLLECTION_ALIAS:
        raise ValueError("ingestion requires a physical collection, not the active alias")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


async def _embed_and_upsert(
    chunks: list[KnowledgeChunkRow],
    *,
    collection: str,
    embedding_client: EmbeddingClient | None,
    milvus_client: MilvusClient | None,
    index_profile: IndexProfile,
) -> None:
    embedder = embedding_client or get_embedding_client()
    vectors = await embedder.embed_texts([row.content for row in chunks])
    rows = [
        {
            "chunk_id": row.chunk_id,
            "document_id": row.document_id,
            "document_type": row.document_type,
            "document_version": row.document_version,
            "title": row.title,
            "section_path": row.section_path,
            "content": row.content,
            "model_id": row.model_id,
            "source_uri": row.source_uri,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "dense_vector": vector,
        }
        for row, vector in zip(chunks, vectors, strict=True)
    ]

    def write_batch() -> None:
        client = milvus_client or milvus.get_client()
        milvus.ensure_collection(client, collection, index_profile=index_profile)
        milvus.upsert_chunks(client, rows, collection)
        milvus.flush(client, collection)

    await milvus.acall(write_batch)
