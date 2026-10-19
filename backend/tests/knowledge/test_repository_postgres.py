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


async def _exercise_concurrent_stage(*, different_content: bool) -> None:
    engine = create_async_engine(os.environ["RISK_DATABASE_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    document_id = f"stage5a-concurrent-{uuid4()}"
    original = _chunk(document_id, "001")
    competing = (
        original.model_copy(update={"content": "different content"})
        if different_content
        else original
    )
    first_staged = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first_writer() -> int:
        async with session_factory() as session:
            async with session.begin():
                staged = await stage_chunks(session, [original])
                first_staged.set()
                await release_first.wait()
                return staged

    async def second_writer() -> int:
        await first_staged.wait()
        async with session_factory() as session:
            async with session.begin():
                second_started.set()
                return await stage_chunks(session, [competing])

    first_task = asyncio.create_task(first_writer())
    second_task = asyncio.create_task(second_writer())
    try:
        await asyncio.wait_for(second_started.wait(), timeout=5)
        done, _ = await asyncio.wait({second_task}, timeout=0.2)
        assert not done, "second writer must wait for the first transaction"
        release_first.set()
        assert await asyncio.wait_for(first_task, timeout=5) == 1
        if different_content:
            with pytest.raises(DocumentVersionConflictError, match="is immutable"):
                await asyncio.wait_for(second_task, timeout=5)
        else:
            assert await asyncio.wait_for(second_task, timeout=5) == 0

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
            assert len(rows) == 1
            assert rows[0].content == original.content
    finally:
        release_first.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.document_id == document_id
                    )
                )
        await engine.dispose()


@pytest.mark.parametrize("different_content", [False, True])
def test_concurrent_stage_serializes_document_version(
    different_content: bool,
) -> None:
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_exercise_concurrent_stage(different_content=different_content))
