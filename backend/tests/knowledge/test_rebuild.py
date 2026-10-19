import asyncio

import pytest

from risk_platform.knowledge import ingestion
from .test_ingestion import (
    FakeEmbeddingClient,
    FakeMilvusClient,
    FakeSession,
    _row,
)


def test_rebuild_pages_pending_and_ready_without_changing_status(monkeypatch) -> None:
    chunks = [_row(suffix) for suffix in ("001", "002", "003")]
    for row, status in zip(chunks, ("READY", "PENDING", "READY"), strict=True):
        row.ingestion_status = status
    pages = []

    async def list_rebuild(_session, *, limit, after_chunk_id):
        pages.append(after_chunk_id)
        remaining = [row for row in chunks if after_chunk_id is None or row.chunk_id > after_chunk_id]
        return remaining[:limit]

    async def forbid_ready(_session, _chunk_ids):
        raise AssertionError("rebuild must not update PostgreSQL status")

    async def run_on_milvus_thread(fn):
        fn()

    monkeypatch.setattr(ingestion, "list_rebuild_chunks", list_rebuild)
    monkeypatch.setattr(ingestion, "mark_chunks_ready", forbid_ready)
    monkeypatch.setattr(ingestion.milvus, "acall", run_on_milvus_thread)
    embedder = FakeEmbeddingClient()
    client = FakeMilvusClient()

    written = asyncio.run(
        ingestion.rebuild_collection(
            collection="riskmind_knowledge_v003",
            batch_size=2,
            session_factory=FakeSession,
            embedding_client=embedder,
            milvus_client=client,
        )
    )

    assert written == 3
    assert pages == [None, "chunk-002", "chunk-003"]
    assert [row.chunk_id for row in chunks] == ["chunk-001", "chunk-002", "chunk-003"]
    assert [row.ingestion_status for row in chunks] == ["READY", "PENDING", "READY"]
    upserts = [call for call in client.calls if call[0] == "upsert"]
    assert [[row["chunk_id"] for row in call[2]] for call in upserts] == [
        ["chunk-001", "chunk-002"],
        ["chunk-003"],
    ]
    assert len([call for call in client.calls if call[0] == "flush"]) == 2
    assert embedder.inputs == [
        ["知识正文 001", "知识正文 002"],
        ["知识正文 003"],
    ]


def test_rebuild_failure_can_rerun_same_physical_collection(monkeypatch) -> None:
    chunks = [_row(suffix) for suffix in ("001", "002", "003")]

    async def list_rebuild(_session, *, limit, after_chunk_id):
        remaining = [row for row in chunks if after_chunk_id is None or row.chunk_id > after_chunk_id]
        return remaining[:limit]

    async def run_on_milvus_thread(fn):
        fn()

    monkeypatch.setattr(ingestion, "list_rebuild_chunks", list_rebuild)
    monkeypatch.setattr(ingestion.milvus, "acall", run_on_milvus_thread)
    client = FakeMilvusClient(fail_upsert_at=2)
    options = dict(
        collection="riskmind_knowledge_v003",
        batch_size=2,
        session_factory=FakeSession,
        embedding_client=FakeEmbeddingClient(),
        milvus_client=client,
    )

    with pytest.raises(RuntimeError, match="Milvus upsert failed"):
        asyncio.run(ingestion.rebuild_collection(**options))
    client.fail_upsert_at = None
    assert asyncio.run(ingestion.rebuild_collection(**options)) == 3

    upserts = [call for call in client.calls if call[0] == "upsert"]
    assert [[row["chunk_id"] for row in call[2]] for call in upserts] == [
        ["chunk-001", "chunk-002"],
        ["chunk-003"],
        ["chunk-001", "chunk-002"],
        ["chunk-003"],
    ]


def test_empty_authoritative_store_creates_an_empty_collection(monkeypatch) -> None:
    async def list_rebuild(_session, *, limit, after_chunk_id):
        return []

    async def run_on_milvus_thread(fn):
        fn()

    monkeypatch.setattr(ingestion, "list_rebuild_chunks", list_rebuild)
    monkeypatch.setattr(ingestion.milvus, "acall", run_on_milvus_thread)
    client = FakeMilvusClient()

    assert asyncio.run(
        ingestion.rebuild_collection(
            collection="riskmind_knowledge_v003",
            session_factory=FakeSession,
            milvus_client=client,
        )
    ) == 0
    assert client.calls == [
        ("has_collection", "riskmind_knowledge_v003"),
        ("load", "riskmind_knowledge_v003"),
    ]
