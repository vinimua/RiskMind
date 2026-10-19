import asyncio
from types import SimpleNamespace

import pytest

from risk_platform.knowledge import ingestion


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self


class FakeEmbeddingClient:
    def __init__(self, *, fail: bool = False):
        self.inputs = []
        self.fail = fail

    async def embed_texts(self, texts):
        self.inputs.append(texts)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[float(index)] * 1024 for index, _ in enumerate(texts)]


class FakeMilvusClient:
    def __init__(self, *, fail_flush: bool = False):
        self.calls = []
        self.fail_flush = fail_flush

    def has_collection(self, collection):
        self.calls.append(("has_collection", collection))
        return True

    def load_collection(self, collection):
        self.calls.append(("load", collection))

    def upsert(self, collection, rows):
        self.calls.append(("upsert", collection, rows))

    def flush(self, collection):
        self.calls.append(("flush", collection))
        if self.fail_flush:
            raise RuntimeError("Milvus flush failed")


def _row(suffix):
    return SimpleNamespace(
        chunk_id=f"chunk-{suffix}",
        document_id="MODEL-DEV-credit_v3",
        document_type="MODEL_DEVELOPMENT_REPORT",
        document_version="v1",
        title="credit_v3 模型开发报告",
        section_path="模型评估 / 评估结果",
        content=f"知识正文 {suffix}",
        model_id="credit_v3",
        source_uri="knowledge://models/credit_v3",
        page_start=None,
        page_end=None,
    )


def _wire_repository(monkeypatch, pending):
    marked = []

    async def list_pending(_session, *, limit):
        return pending[:limit]

    async def mark_ready(_session, chunk_ids):
        marked.extend(chunk_ids)
        pending[:] = [row for row in pending if row.chunk_id not in chunk_ids]
        return len(chunk_ids)

    async def run_on_milvus_thread(fn):
        fn()

    monkeypatch.setattr(ingestion, "list_pending_chunks", list_pending)
    monkeypatch.setattr(ingestion, "mark_chunks_ready", mark_ready)
    monkeypatch.setattr(ingestion.milvus, "acall", run_on_milvus_thread)
    return marked


def test_ingest_pending_batches_and_marks_ready_after_flush(monkeypatch) -> None:
    pending = [_row("001"), _row("002"), _row("003")]
    marked = _wire_repository(monkeypatch, pending)
    embedder = FakeEmbeddingClient()
    client = FakeMilvusClient()

    completed = asyncio.run(
        ingestion.ingest_pending(
            collection="riskmind_knowledge_v003",
            batch_size=2,
            session_factory=FakeSession,
            embedding_client=embedder,
            milvus_client=client,
        )
    )

    assert completed == 3
    assert pending == []
    assert marked == ["chunk-001", "chunk-002", "chunk-003"]
    assert embedder.inputs == [
        ["知识正文 001", "知识正文 002"],
        ["知识正文 003"],
    ]
    upserts = [call for call in client.calls if call[0] == "upsert"]
    assert [len(call[2]) for call in upserts] == [2, 1]
    assert upserts[0][2][0]["chunk_id"] == "chunk-001"
    assert upserts[0][2][0]["dense_vector"] == [0.0] * 1024
    assert [call[1] for call in client.calls if call[0] == "flush"] == [
        "riskmind_knowledge_v003",
        "riskmind_knowledge_v003",
    ]


def test_failed_flush_leaves_batch_pending_for_idempotent_retry(monkeypatch) -> None:
    pending = [_row("001")]
    marked = _wire_repository(monkeypatch, pending)
    embedder = FakeEmbeddingClient()
    client = FakeMilvusClient(fail_flush=True)
    options = dict(
        collection="riskmind_knowledge_v003",
        session_factory=FakeSession,
        embedding_client=embedder,
        milvus_client=client,
    )

    with pytest.raises(RuntimeError, match="Milvus flush failed"):
        asyncio.run(ingestion.ingest_pending(**options))
    assert marked == []
    assert [row.chunk_id for row in pending] == ["chunk-001"]

    client.fail_flush = False
    assert asyncio.run(ingestion.ingest_pending(**options)) == 1
    upserts = [call for call in client.calls if call[0] == "upsert"]
    assert [call[2][0]["chunk_id"] for call in upserts] == [
        "chunk-001",
        "chunk-001",
    ]
    assert marked == ["chunk-001"]


def test_embedding_failure_does_not_write_or_mark_ready(monkeypatch) -> None:
    pending = [_row("001")]
    marked = _wire_repository(monkeypatch, pending)
    client = FakeMilvusClient()

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        asyncio.run(
            ingestion.ingest_pending(
                collection="riskmind_knowledge_v003",
                session_factory=FakeSession,
                embedding_client=FakeEmbeddingClient(fail=True),
                milvus_client=client,
            )
        )

    assert client.calls == []
    assert marked == []
    assert len(pending) == 1


def test_ingestion_rejects_alias_and_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="physical collection"):
        asyncio.run(
            ingestion.ingest_pending(collection=ingestion.milvus.ACTIVE_COLLECTION_ALIAS)
        )
    with pytest.raises(ValueError, match="batch_size"):
        asyncio.run(ingestion.ingest_pending(collection="physical", batch_size=65))
