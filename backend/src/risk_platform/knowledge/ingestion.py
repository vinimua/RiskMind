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
)


DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 64


async def ingest_pending(
    *,
    collection: str,
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

    sessions = session_factory or get_session_factory()
    completed = 0
    while True:
        async with sessions() as session:
            pending = await list_pending_chunks(session, limit=batch_size)
        if not pending:
            return completed

        await _embed_and_upsert(
            pending,
            collection=collection,
            embedding_client=embedding_client,
            milvus_client=milvus_client,
        )

        async with sessions() as session:
            async with session.begin():
                completed += await mark_chunks_ready(
                    session, [row.chunk_id for row in pending]
                )


async def rebuild_collection(
    *,
    collection: str,
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
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        expected_count = await count_chunks(session)
    after_chunk_id = None
    written = 0
    while True:
        async with sessions() as session:
            chunks = await list_rebuild_chunks(
                session, limit=batch_size, after_chunk_id=after_chunk_id
            )
        if not chunks:
            if written == 0:
                def ensure_empty_collection() -> None:
                    client = milvus_client or milvus.get_client()
                    milvus.ensure_collection(client, collection)

                await milvus.acall(ensure_empty_collection)
            async with sessions() as session:
                actual_count = await count_chunks(session)
            if actual_count != expected_count:
                raise RuntimeError(
                    "authoritative knowledge changed during rebuild; rerun rebuild"
                )
            return written

        await _embed_and_upsert(
            chunks,
            collection=collection,
            embedding_client=embedding_client,
            milvus_client=milvus_client,
        )
        written += len(chunks)
        after_chunk_id = chunks[-1].chunk_id


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
        milvus.ensure_collection(client, collection)
        milvus.upsert_chunks(client, rows, collection)
        milvus.flush(client, collection)

    await milvus.acall(write_batch)
