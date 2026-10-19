import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow
from risk_platform.knowledge.repository import (
    DocumentVersionConflictError,
    mark_chunks_ready,
    stage_chunks,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 to run PostgreSQL integration tests",
)


def _chunk(document_id: str, suffix: str, *, version: str = "v1") -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=f"{document_id}-{version}-{suffix}",
        document_id=document_id,
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        document_version=version,
        title="credit_v3 模型开发报告",
        section_path="模型评估 / 评估结果",
        content=f"第 {suffix} 个知识块",
        model_id="credit_v3",
        source_uri="minio://riskmind/models/credit_v3/v1.pdf",
        page_start=7,
        page_end=8,
    )


async def _exercise_repository() -> None:
    database_url = os.environ["RISK_DATABASE_URL"]
    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    document_id = f"stage5a-{uuid4()}"
    chunks = [_chunk(document_id, "001"), _chunk(document_id, "002")]

    try:
        with pytest.raises(RuntimeError, match="caller rollback"):
            async with session_factory() as session:
                async with session.begin():
                    assert await stage_chunks(session, chunks) == 2
                    raise RuntimeError("caller rollback")

        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeChunkRow).where(
                            KnowledgeChunkRow.document_id == document_id
                        )
                    )
                ).scalars()
            )
            assert rows == []

        async with session_factory() as session:
            async with session.begin():
                assert await stage_chunks(session, chunks) == 2

        async with session_factory() as session:
            async with session.begin():
                assert await mark_chunks_ready(
                    session, [chunk.chunk_id for chunk in chunks]
                ) == 2

        async with session_factory() as session:
            async with session.begin():
                assert await stage_chunks(session, chunks) == 0

        changed = chunks[0].model_copy(update={"content": "被错误修改的正文"})
        async with session_factory() as session:
            with pytest.raises(DocumentVersionConflictError, match="is immutable"):
                async with session.begin():
                    await stage_chunks(session, [changed, chunks[1]])

        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeChunkRow)
                        .where(KnowledgeChunkRow.document_id == document_id)
                        .order_by(KnowledgeChunkRow.chunk_id)
                    )
                ).scalars()
            )
            assert len(rows) == 2
            assert all(
                row.ingestion_status == IngestionStatus.READY.value for row in rows
            )
            assert rows[0].content == chunks[0].content
    finally:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.document_id == document_id
                    )
                )
        await engine.dispose()


def test_repository_transactions_and_document_version_immutability() -> None:
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_exercise_repository())
