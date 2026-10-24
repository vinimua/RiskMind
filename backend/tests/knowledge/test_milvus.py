import asyncio
import threading
from types import SimpleNamespace

import pytest

from risk_platform.knowledge import milvus
from risk_platform.knowledge.contracts import (
    KnowledgeDocumentType,
    KnowledgeFilters,
)


def _vector(axis: int = 0) -> list[float]:
    vector = [0.0] * milvus.EMBEDDING_DIMENSION
    vector[axis] = 1.0
    return vector


def test_acall_uses_one_dedicated_milvus_thread() -> None:
    async def run_calls() -> list[str]:
        return await asyncio.gather(
            *(milvus.acall(lambda: threading.current_thread().name) for _ in range(3))
        )

    thread_names = asyncio.run(run_calls())

    assert len(set(thread_names)) == 1
    assert thread_names[0].startswith("milvus")


def test_acall_creates_and_reuses_client_on_the_dedicated_thread(
    monkeypatch,
) -> None:
    created_on: list[str] = []

    def fake_client(*, uri: str):
        created_on.append(threading.current_thread().name)
        return SimpleNamespace(uri=uri)

    async def run_calls():
        def work():
            return milvus.get_client(), threading.current_thread().name

        return await asyncio.gather(milvus.acall(work), milvus.acall(work))

    monkeypatch.setattr(milvus, "_CLIENT", None)
    monkeypatch.setattr(milvus, "MilvusClient", fake_client)

    first, second = asyncio.run(run_calls())

    assert first[0] is second[0]
    assert first[1] == second[1]
    assert first[1].startswith("milvus")
    assert created_on == [first[1]]


class FakeSchema:
    def __init__(self):
        self.fields: list[tuple[str, object, dict]] = []
        self.functions: list[object] = []

    def add_field(self, name: str, data_type: object, **kwargs) -> None:
        self.fields.append((name, data_type, kwargs))

    def add_function(self, function: object) -> None:
        self.functions.append(function)


class FakeIndexes:
    def __init__(self):
        self.values: list[dict] = []

    def add_index(self, **kwargs) -> None:
        self.values.append(kwargs)


class FakeMilvusClient:
    def __init__(self, *, collection_exists: bool = False):
        self.collection_exists = collection_exists
        self.schema = FakeSchema()
        self.indexes = FakeIndexes()
        self.schema_options: dict = {}
        self.created: list[dict] = []
        self.loaded: list[str] = []
        self.upserts: list[tuple[str, list[dict]]] = []
        self.flushed: list[str] = []
        self.search_calls: list[dict] = []
        self.search_results: list[list[dict]] = []

    def has_collection(self, collection: str) -> bool:
        return self.collection_exists

    def describe_collection(self, collection: str):
        return {"collection_name": collection, "description": self.schema_options.get("description", "")}

    def create_schema(self, **kwargs) -> FakeSchema:
        self.schema_options = kwargs
        return self.schema

    def prepare_index_params(self) -> FakeIndexes:
        return self.indexes

    def create_collection(self, collection: str, **kwargs) -> None:
        self.created.append({"collection": collection, **kwargs})
        self.collection_exists = True

    def load_collection(self, collection: str) -> None:
        self.loaded.append(collection)

    def upsert(self, collection: str, rows: list[dict]) -> None:
        self.upserts.append((collection, rows))

    def flush(self, collection: str) -> None:
        self.flushed.append(collection)

    def search(self, collection: str, **kwargs):
        self.search_calls.append({"collection": collection, **kwargs})
        return self.search_results


def test_get_client_reuses_the_process_client(monkeypatch) -> None:
    created = []

    def fake_client(*, uri: str):
        client = SimpleNamespace(uri=uri)
        created.append(client)
        return client

    monkeypatch.setattr(milvus, "_CLIENT", None)
    monkeypatch.setenv("MILVUS_URI", "http://milvus.test:19530")
    monkeypatch.setattr(milvus, "MilvusClient", fake_client)

    first = milvus.get_client()
    second = milvus.get_client()

    assert first is second
    assert len(created) == 1
    assert first.uri == "http://milvus.test:19530"


def test_get_client_with_explicit_uri_is_independent(monkeypatch) -> None:
    created = []

    def fake_client(*, uri: str):
        client = SimpleNamespace(uri=uri)
        created.append(client)
        return client

    monkeypatch.setattr(milvus, "MilvusClient", fake_client)

    first = milvus.get_client("http://test-one:19530")
    second = milvus.get_client("http://test-two:19530")

    assert first is not second
    assert [client.uri for client in created] == [
        "http://test-one:19530",
        "http://test-two:19530",
    ]


def test_ensure_collection_creates_the_frozen_schema_and_indexes() -> None:
    client = FakeMilvusClient()

    milvus.ensure_collection(client, "knowledge_test")

    assert client.schema_options == {
        "auto_id": False,
        "enable_dynamic_field": False,
    }
    fields = {name: options for name, _, options in client.schema.fields}
    assert list(fields) == [
        "chunk_id",
        "document_id",
        "document_type",
        "document_version",
        "title",
        "section_path",
        "content",
        "model_id",
        "source_uri",
        "page_start",
        "page_end",
        "dense_vector",
        "sparse_vector",
    ]
    assert fields["chunk_id"] == {"is_primary": True, "max_length": 128}
    assert fields["content"]["analyzer_params"] == milvus.CONTENT_ANALYZER
    assert fields["dense_vector"]["dim"] == 1024
    assert fields["model_id"]["nullable"] is True
    assert fields["source_uri"] == {"max_length": 2048}
    assert fields["page_start"] == {"nullable": True}
    assert fields["page_end"] == {"nullable": True}
    assert len(client.schema.functions) == 1
    assert client.indexes.values == [
        {
            "field_name": "dense_vector",
            "index_type": "HNSW",
            "metric_type": "COSINE",
            "params": {"M": 16, "efConstruction": 200},
        },
        {
            "field_name": "sparse_vector",
            "index_type": "SPARSE_INVERTED_INDEX",
            "metric_type": "BM25",
            "params": {
                "inverted_index_algo": "DAAT_MAXSCORE",
                "bm25_k1": 1.2,
                "bm25_b": 0.75,
            },
        },
    ]
    assert client.loaded == ["knowledge_test"]


def test_ensure_collection_loads_an_existing_collection_without_recreating() -> None:
    client = FakeMilvusClient(collection_exists=True)

    milvus.ensure_collection(client, "knowledge_test")

    assert client.created == []
    assert client.loaded == ["knowledge_test"]


def test_process_collection_is_ensured_only_once(monkeypatch) -> None:
    client = FakeMilvusClient()
    monkeypatch.setattr(milvus, "_CLIENT", client)
    monkeypatch.setattr(milvus, "_ENSURED_COLLECTIONS", set())

    milvus.ensure_collection(client, "knowledge_test")
    milvus.ensure_collection(client, "knowledge_test")

    assert len(client.created) == 1
    assert client.loaded == ["knowledge_test"]


def test_upsert_and_flush_use_the_selected_collection() -> None:
    client = FakeMilvusClient()
    rows = [{"chunk_id": "chunk-001"}]

    milvus.upsert_chunks(client, rows, "knowledge_test")
    milvus.flush(client, "knowledge_test")

    assert client.upserts == [("knowledge_test", rows)]
    assert client.flushed == ["knowledge_test"]


def test_empty_upsert_does_not_call_milvus() -> None:
    client = FakeMilvusClient()

    milvus.upsert_chunks(client, [], "knowledge_test")

    assert client.upserts == []


def test_dense_search_returns_metadata_and_uses_contract_filters() -> None:
    client = FakeMilvusClient()
    entity = {
        "chunk_id": "chunk-001",
        "document_id": "document-001",
        "document_type": "INCIDENT_REPORT",
        "document_version": "v1",
        "title": "credit_v3 KS 下降调查",
        "section_path": "调查过程/标签成熟度",
        "content": "T+30 标签成熟后 KS 恢复。",
        "model_id": "credit_v3",
        "source_uri": "minio://riskmind/cases/CASE-2026-018.pdf",
        "page_start": 7,
        "page_end": 8,
    }
    client.search_results = [[{"distance": 0.93, "entity": entity}]]
    filters = KnowledgeFilters(
        document_types=[KnowledgeDocumentType.INCIDENT_REPORT],
        document_id="document-001",
        model_id="credit_v3",
        document_version="v1",
    )

    hits = milvus.dense_search(
        client,
        _vector(),
        top_k=5,
        filters=filters,
        collection="knowledge_test",
    )

    assert hits == [{**entity, "score": 0.93}]
    assert client.search_calls == [
        {
            "collection": "knowledge_test",
            "data": [_vector()],
            "anns_field": "dense_vector",
            "limit": 5,
            "output_fields": milvus.OUTPUT_FIELDS,
            "search_params": {
                "metric_type": "COSINE",
                "params": {"ef": 64},
            },
            "filter": (
                'document_type in ["INCIDENT_REPORT"]'
                ' and document_id == "document-001"'
                ' and model_id == "credit_v3"'
                ' and document_version == "v1"'
            ),
        }
    ]


def test_dense_search_uses_active_alias_and_expands_ef_for_large_top_k() -> None:
    client = FakeMilvusClient()

    assert milvus.dense_search(client, _vector(), top_k=100) == []

    assert client.search_calls[0]["collection"] == milvus.ACTIVE_COLLECTION_ALIAS
    assert client.search_calls[0]["search_params"] == {
        "metric_type": "COSINE",
        "params": {"ef": 100},
    }


@pytest.mark.parametrize(
    ("vector", "top_k", "error"),
    [
        ([0.1], 5, "1024 dimensions"),
        (_vector(), 0, "top_k must be positive"),
    ],
)
def test_dense_search_rejects_invalid_inputs(
    vector: list[float],
    top_k: int,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        milvus.dense_search(FakeMilvusClient(), vector, top_k)
