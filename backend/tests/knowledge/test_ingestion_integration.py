import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge import ingestion, milvus
from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.ingestion import ingest_pending
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow, KnowledgeCollectionRow
from risk_platform.knowledge.repository import stage_chunks


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INGESTION_INTEGRATION") != "1",
    reason="set RUN_INGESTION_INTEGRATION=1 for PostgreSQL and Milvus",
)


class FixedEmbeddingClient:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _ in texts]


async def _exercise_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(os.environ["RISK_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex
    document_id = f"MODEL-DEV-stage5b-{suffix}"
    collection = f"riskmind_stage5b_test_{suffix}"
    chunk = KnowledgeChunk(
        chunk_id=f"chunk-{suffix}",
        document_id=document_id,
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        document_version="v1",
        title="credit_v3 模型开发报告",
        section_path="模型评估 / 评估结果",
        content="credit_v3 的 KS 和 AUC 评估结果。",
        model_id="credit_v3",
        source_uri="knowledge://models/credit_v3",
    )

    try:
        async with sessions() as session:
            async with session.begin():
                assert await stage_chunks(session, [chunk]) == 1

        original_mark_ready = ingestion.mark_chunks_ready

        async def fail_mark_ready(_session, _chunk_ids, **_kwargs):
            raise RuntimeError("database update failed")

        monkeypatch.setattr(ingestion, "mark_chunks_ready", fail_mark_ready)
        with pytest.raises(RuntimeError, match="database update failed"):
            await ingest_pending(
                collection=collection,
                session_factory=sessions,
                embedding_client=FixedEmbeddingClient(),
            )
        monkeypatch.setattr(ingestion, "mark_chunks_ready", original_mark_ready)

        async with sessions() as session:
            row = (
                await session.execute(
                    select(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.chunk_id == chunk.chunk_id
                    )
                )
            ).scalar_one()
            assert row.ingestion_status == IngestionStatus.PENDING.value

        def read_milvus():
            return milvus.dense_search(
                milvus.get_client(),
                [1.0] + [0.0] * 1023,
                top_k=1,
                collection=collection,
            )

        hits_before_retry = await milvus.acall(read_milvus)
        assert len(hits_before_retry) == 1
        assert hits_before_retry[0]["chunk_id"] == chunk.chunk_id

        assert await ingest_pending(
            collection=collection,
            session_factory=sessions,
            embedding_client=FixedEmbeddingClient(),
        ) == 1
        assert await ingest_pending(
            collection=collection,
            session_factory=sessions,
            embedding_client=FixedEmbeddingClient(),
        ) == 0

        async with sessions() as session:
            row = (
                await session.execute(
                    select(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.chunk_id == chunk.chunk_id
                    )
                )
            ).scalar_one()
            assert row.ingestion_status == IngestionStatus.READY.value
            assert row.ready_at is not None

        hits = await milvus.acall(read_milvus)
        assert len(hits) == 1
        assert hits[0]["chunk_id"] == chunk.chunk_id
        assert hits[0]["source_uri"] == chunk.source_uri
    finally:
        async with sessions() as session:
            async with session.begin():
                await session.execute(delete(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.collection == collection,
                ))
                await session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.document_id == document_id
                    )
                )
        await engine.dispose()

        def drop_collection():
            client = milvus.get_client()
            if client.has_collection(collection):
                client.drop_collection(collection)

        await milvus.acall(drop_collection)


def test_postgres_pending_to_milvus_upsert_to_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_exercise_ingestion(monkeypatch))
