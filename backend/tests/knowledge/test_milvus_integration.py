import os
from uuid import uuid4

import pytest

from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.milvus import (
    EMBEDDING_DIMENSION,
    dense_search,
    ensure_collection,
    flush,
    get_client,
    upsert_chunks,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MILVUS_INTEGRATION") != "1",
    reason="set RUN_MILVUS_INTEGRATION=1 to run against local Milvus",
)


def _vector(axis: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[axis] = 1.0
    return vector


def test_collection_upsert_flush_and_dense_search() -> None:
    client = get_client(os.environ.get("MILVUS_URI", "http://localhost:19530"))
    collection = f"riskmind_stage3_test_{uuid4().hex}"
    rows = [
        {
            "chunk_id": "chunk-credit-v3",
            "document_id": "document-001",
            "document_type": "INCIDENT_REPORT",
            "document_version": "v1",
            "title": "credit_v3 KS 下降调查",
            "section_path": "调查过程/标签成熟度",
            "content": "T+30 标签成熟后 KS 恢复。",
            "model_id": "credit_v3",
            "dense_vector": _vector(0),
        },
        {
            "chunk_id": "chunk-credit-v2",
            "document_id": "document-002",
            "document_type": "INCIDENT_REPORT",
            "document_version": "v1",
            "title": "credit_v2 KS 下降调查",
            "section_path": "调查过程/数据质量",
            "content": "修复缺失数据源后 KS 恢复。",
            "model_id": "credit_v2",
            "dense_vector": _vector(1),
        },
    ]

    try:
        ensure_collection(client, collection)
        ensure_collection(client, collection)
        upsert_chunks(client, rows, collection)
        flush(client, collection)

        hits = dense_search(
            client,
            _vector(0),
            top_k=2,
            filters=KnowledgeFilters(model_id="credit_v3"),
            collection=collection,
        )

        assert [hit["chunk_id"] for hit in hits] == ["chunk-credit-v3"]
        assert hits[0]["document_id"] == "document-001"
        assert hits[0]["model_id"] == "credit_v3"
        assert hits[0]["score"] == pytest.approx(1.0)
    finally:
        if client.has_collection(collection):
            client.drop_collection(collection)
