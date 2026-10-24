import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge import cli, ingestion, milvus
from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeCollectionRow


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INGESTION_INTEGRATION") != "1",
    reason="set RUN_INGESTION_INTEGRATION=1 for PostgreSQL and Milvus",
)


class FixedEmbeddingClient:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1023 for _ in texts]


async def _inspect_database(document_ids: set[str]) -> dict[str, str]:
    engine = create_async_engine(os.environ["RISK_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeChunkRow).where(
                            KnowledgeChunkRow.document_id.in_(document_ids)
                        )
                    )
                ).scalars()
            )
            return {row.document_id: row.ingestion_status for row in rows}
    finally:
        await engine.dispose()


async def _cleanup(document_ids: set[str], collections: tuple[str, str]) -> None:
    engine = create_async_engine(os.environ["RISK_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            async with session.begin():
                await session.execute(delete(KnowledgeCollectionRow).where(
                    KnowledgeCollectionRow.collection.in_(collections),
                ))
                await session.execute(
                    delete(KnowledgeChunkRow).where(
                        KnowledgeChunkRow.document_id.in_(document_ids)
                    )
                )
    finally:
        await engine.dispose()

    def drop_collections() -> None:
        client = milvus.get_client()
        for collection in collections:
            if client.has_collection(collection):
                client.drop_collection(collection)

    await milvus.acall(drop_collections)


def test_cli_stage_resume_and_rebuild_preserves_postgres_statuses(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(ingestion, "get_embedding_client", FixedEmbeddingClient)
    suffix = uuid4().hex
    ready_document = f"MODEL-DEV-{suffix}"
    pending_document = f"CASE-{suffix}"
    document_ids = {ready_document, pending_document}
    resume_collection = f"riskmind_stage5c_resume_{suffix}"
    rebuilt_collection = f"riskmind_stage5c_rebuild_{suffix}"
    collections = (resume_collection, rebuilt_collection)
    ready_path = tmp_path / "model.md"
    pending_path = tmp_path / "incident.md"
    ready_path.write_text(
        "# credit_v3 模型开发报告\n\n## 模型评估\nKS 为 0.42。",
        encoding="utf-8",
    )
    pending_path.write_text(
        "# credit_v3 异常调查报告\n\n## 根因结论\n标签成熟度不足。",
        encoding="utf-8",
    )

    def stage(path, document_id, document_type, title):
        return cli.main(
            [
                "stage", str(path),
                "--document-id", document_id,
                "--document-version", "v1",
                "--document-type", document_type,
                "--title", title,
                "--source-uri", f"knowledge://stage5c/{document_id}",
                "--model-id", "credit_v3",
            ]
        )

    try:
        assert stage(
            ready_path, ready_document, "MODEL_DEVELOPMENT_REPORT",
            "credit_v3 模型开发报告",
        ) == 0
        assert stage(
            ready_path, ready_document, "MODEL_DEVELOPMENT_REPORT",
            "credit_v3 模型开发报告",
        ) == 0
        assert cli.main(["resume", "--collection", resume_collection]) == 0
        assert stage(
            pending_path, pending_document, "INCIDENT_REPORT",
            "credit_v3 异常调查报告",
        ) == 0

        before = asyncio.run(_inspect_database(document_ids))
        assert before == {ready_document: "READY", pending_document: "PENDING"}

        assert cli.main(["rebuild", "--collection", rebuilt_collection]) == 0
        assert asyncio.run(_inspect_database(document_ids)) == before

        def search_rebuilt():
            return milvus.dense_search(
                milvus.get_client(),
                [1.0] + [0.0] * 1023,
                top_k=2,
                collection=rebuilt_collection,
            )

        hits = asyncio.run(milvus.acall(search_rebuilt))
        assert {hit["document_id"] for hit in hits} == document_ids
    finally:
        asyncio.run(_cleanup(document_ids, collections))
