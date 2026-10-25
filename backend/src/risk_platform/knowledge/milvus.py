
import asyncio
import functools
import json
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pymilvus import DataType, Function, FunctionType, MilvusClient

from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.embeddings import EMBEDDING_DIMENSION
from risk_platform.knowledge.indexing.profiles import IndexProfile


DEFAULT_MILVUS_URI = "http://localhost:19530"
ACTIVE_COLLECTION_ALIAS = "riskmind_knowledge_active"

CONTENT_ANALYZER = {"tokenizer": "icu", "filter": ["lowercase"]}
DENSE_SEARCH_EF = 64

OUTPUT_FIELDS = [
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
]

_CLIENT: MilvusClient | None = None
_ENSURED_COLLECTIONS: set[str] = set()
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="milvus")


async def acall(fn, *args, **kwargs):
    """Run one synchronous Milvus operation on its dedicated thread."""

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _EXECUTOR,
        functools.partial(fn, *args, **kwargs),
    )


def get_client(uri: str | None = None) -> MilvusClient:
    """Return the process client, or an independent client for an explicit URI."""

    global _CLIENT
    if uri is not None:
        return MilvusClient(uri=uri)
    if _CLIENT is None:
        _CLIENT = MilvusClient(
            uri=os.environ.get("MILVUS_URI", DEFAULT_MILVUS_URI)
        )
    return _CLIENT


def validate_collection_for_profile(
    client: MilvusClient,
    collection: str,
    index_profile: IndexProfile,
) -> None:
    """Read-only compatibility check; never create, load or modify a collection."""
    profile = IndexProfile(index_profile)
    if not client.has_collection(collection):
        return
    _validate_collection_description(client.describe_collection(collection), collection, profile)


def _validate_collection_description(
    description: dict[str, Any], collection: str, profile: IndexProfile,
) -> None:
    if description["collection_name"] != collection:
        raise ValueError("index writes require a physical collection, not an alias")
    owner = description.get("description", "")
    if owner != f"riskmind:index-profile:{profile.value}" and not (
        not owner.startswith("riskmind:index-profile:") and profile == IndexProfile.BASELINE
    ):
        raise ValueError("Milvus collection has another or unknown index profile")


def ensure_collection(
    client: MilvusClient,
    collection: str,
    *,
    index_profile: IndexProfile | None = None,
) -> None:
    """Create the frozen schema if absent; otherwise load without migration."""

    if index_profile is None and client is _CLIENT and collection in _ENSURED_COLLECTIONS:
        return

    if client.has_collection(collection):
        if index_profile is not None:
            # Recheck at write time as well as before the PostgreSQL binding.
            _validate_collection_description(
                client.describe_collection(collection), collection, IndexProfile(index_profile),
            )
        client.load_collection(collection)
    else:
        schema = client.create_schema(
            auto_id=False, enable_dynamic_field=False,
            **({"description": f"riskmind:index-profile:{index_profile.value}"}
               if index_profile is not None else {}),
        )
        schema.add_field(
            "chunk_id",
            DataType.VARCHAR,
            is_primary=True,
            max_length=128,
        )
        schema.add_field("document_id", DataType.VARCHAR, max_length=128)
        schema.add_field("document_type", DataType.VARCHAR, max_length=64)
        schema.add_field("document_version", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("section_path", DataType.VARCHAR, max_length=1024)
        schema.add_field(
            "content",
            DataType.VARCHAR,
            max_length=16384,
            enable_analyzer=True,
            analyzer_params=CONTENT_ANALYZER,
        )
        schema.add_field(
            "model_id",
            DataType.VARCHAR,
            max_length=128,
            nullable=True,
        )
        schema.add_field("source_uri", DataType.VARCHAR, max_length=2048)
        schema.add_field("page_start", DataType.INT64, nullable=True)
        schema.add_field("page_end", DataType.INT64, nullable=True)
        schema.add_field(
            "dense_vector",
            DataType.FLOAT_VECTOR,
            dim=EMBEDDING_DIMENSION,
        )
        schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(
            Function(
                name="content_bm25",
                input_field_names=["content"],
                output_field_names=["sparse_vector"],
                function_type=FunctionType.BM25,
            )
        )

        indexes = client.prepare_index_params()
        indexes.add_index(
            field_name="dense_vector",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        indexes.add_index(
            field_name="sparse_vector",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={
                "inverted_index_algo": "DAAT_MAXSCORE",
                "bm25_k1": 1.2,
                "bm25_b": 0.75,
            },
        )
        client.create_collection(
            collection,
            schema=schema,
            index_params=indexes,
        )
        client.load_collection(collection)

    if client is _CLIENT:
        _ENSURED_COLLECTIONS.add(collection)


def upsert_chunks(
    client: MilvusClient,
    rows: list[dict[str, Any]],
    collection: str,
) -> None:
    if rows:
        client.upsert(collection, rows)


def flush(
    client: MilvusClient,
    collection: str,
) -> None:
    client.flush(collection)


def dense_search(
    client: MilvusClient,
    vector: list[float],
    top_k: int,
    filters: KnowledgeFilters | None = None,
    collection: str = ACTIVE_COLLECTION_ALIAS,
) -> list[dict[str, Any]]:
    """Search one dense vector and return metadata plus its cosine score."""

    if len(vector) != EMBEDDING_DIMENSION:
        raise ValueError(
            f"dense query vector must have {EMBEDDING_DIMENSION} dimensions"
        )
    if top_k <= 0:
        raise ValueError("dense search top_k must be positive")

    results = client.search(
        collection,
        data=[vector],
        anns_field="dense_vector",
        limit=top_k,
        output_fields=OUTPUT_FIELDS,
        search_params={
            "metric_type": "COSINE",
            "params": {"ef": max(DENSE_SEARCH_EF, top_k)},
        },
        filter=_filter_expression(filters),
    )
    if not results:
        return []

    hits = []
    for result in results[0]:
        entity = result["entity"]
        hit = {field: entity[field] for field in OUTPUT_FIELDS}
        hit["score"] = float(result["distance"])
        hits.append(hit)
    return hits


def _filter_expression(filters: KnowledgeFilters | None) -> str:
    if filters is None:
        return ""

    clauses = []
    if filters.document_types:
        values = [document_type.value for document_type in filters.document_types]
        clauses.append(f"document_type in {json.dumps(values)}")
    for field in ("document_id", "model_id", "document_version"):
        value = getattr(filters, field)
        if value is not None:
            clauses.append(f"{field} == {json.dumps(value, ensure_ascii=False)}")
    return " and ".join(clauses)
# 先限定：
# CASE-2026-018 + credit_v3 + 指定版本 + 事故报告
# 再在限定范围内：
# 执行 Dense COSINE 检索
