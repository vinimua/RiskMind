"""Move pending knowledge chunks into the Milvus read model."""

from __future__ import annotations

from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.embeddings import EmbeddingClient, get_embedding_client
from risk_platform.knowledge.repository import list_pending_chunks, mark_chunks_ready


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

    if not collection or not collection.strip():
        raise ValueError("a physical collection name is required")
    if collection == milvus.ACTIVE_COLLECTION_ALIAS:
        raise ValueError("ingestion requires a physical collection, not the active alias")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")

    sessions = session_factory or get_session_factory()
    completed = 0
    while True:
        async with sessions() as session:
            pending = await list_pending_chunks(session, limit=batch_size)
        if not pending:
            return completed

        embedder = embedding_client or get_embedding_client()
        vectors = await embedder.embed_texts([row.content for row in pending])
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
            for row, vector in zip(pending, vectors, strict=True)
        ]

        def write_batch() -> None:
            client = milvus_client or milvus.get_client()
            milvus.ensure_collection(client, collection)
            milvus.upsert_chunks(client, rows, collection)
            milvus.flush(client, collection)

        await milvus.acall(write_batch)

        async with sessions() as session:
            async with session.begin():
                completed += await mark_chunks_ready(
                    session, [row.chunk_id for row in pending]
                )
