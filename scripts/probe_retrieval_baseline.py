"""Stage 0 disposable probe for the RiskMind retrieval baseline.

This is deliberately not a production repository/service. It validates the
chosen embedding, Milvus schema, analyzers, BM25, and alias switching.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from openai import OpenAI
from pymilvus import DataType, Function, FunctionType, MilvusClient, __version__


EMBEDDING_DIMENSION = 1024
PHYSICAL_V1 = "riskmind_knowledge_v001"
PHYSICAL_V2 = "riskmind_knowledge_v002"
ACTIVE_ALIAS = "riskmind_knowledge_active"

ANALYZERS: dict[str, dict[str, Any]] = {
    "chinese_builtin": {"type": "chinese"},
    "jieba_mixed": {
        "tokenizer": "jieba",
        "filter": ["lowercase", "removepunct"],
    },
    "icu_mixed": {"tokenizer": "icu", "filter": ["lowercase"]},
    "standard": {"type": "standard"},
}
SELECTED_ANALYZER = "icu_mixed"

ANALYZER_TEXTS = [
    "credit_v3 最近 KS 从 0.42 降到 0.31",
    "CASE-2026-018 的 apply_cnt_30d 出现 PSI 漂移",
    "模型 v3.2 在 T+30 标签成熟后性能恢复",
    "credit_v3 和 credit_v2 的 KS 下降案例",
]

CORPUS = [
    {
        "chunk_id": "ks_label_maturity",
        "document_id": "doc_case_001",
        "document_type": "incident_report",
        "title": "credit_v3 标签成熟度调查",
        "section_path": "调查过程/标签成熟度",
        "content": "credit_v3 最近 KS 从 0.42 降到 0.31。T+30 标签成熟后 KS 恢复，根因是评估窗口中的标签尚未成熟。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-001",
    },
    {
        "chunk_id": "feature_psi_apply_cnt",
        "document_id": "doc_case_018",
        "document_type": "incident_report",
        "title": "apply_cnt_30d 特征漂移",
        "section_path": "调查过程/特征漂移",
        "content": "CASE-2026-018 的 apply_cnt_30d 出现 PSI 漂移，近期申请频次分布变化导致模型区分能力下降。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-018",
    },
    {
        "chunk_id": "population_shift",
        "document_id": "doc_case_022",
        "document_type": "incident_report",
        "title": "渠道客群结构变化",
        "section_path": "根因/客群迁移",
        "content": "渠道 C 新客占比显著增加，客群结构变化造成 credit_v3 的 KS 下降。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-022",
    },
    {
        "chunk_id": "model_v32_incident",
        "document_id": "doc_case_025",
        "document_type": "incident_report",
        "title": "credit_v3 v3.2 版本异常",
        "section_path": "根因/版本",
        "content": "credit_v3 v3.2 发布时特征流水线版本未同步，回滚后 AUC 与 KS 恢复。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-025",
    },
    {
        "chunk_id": "model_known_limit",
        "document_id": "doc_model_v3",
        "document_type": "model_report",
        "title": "credit_v3 已知限制",
        "section_path": "适用范围/限制",
        "content": "credit_v3 在渠道 C 的薄文件新客上稳定性有限，需持续监控 PSI 和分群 KS。",
        "model_id": "credit_v3",
        "case_id": "",
    },
    {
        "chunk_id": "credit_v2_ks_case",
        "document_id": "doc_case_v2",
        "document_type": "incident_report",
        "title": "credit_v2 KS 下降案例",
        "section_path": "历史案例/credit_v2",
        "content": "credit_v2 曾因收入特征缺失率上升发生 KS 下降，修复数据源后恢复。",
        "model_id": "credit_v2",
        "case_id": "CASE-2025-031",
    },
    {
        "chunk_id": "feature_missing_rate",
        "document_id": "doc_case_030",
        "document_type": "incident_report",
        "title": "特征缺失率异常",
        "section_path": "调查过程/数据质量",
        "content": "变量 bureau_score 缺失率由 3% 上升到 27%，模型 AUC 与 KS 同时下降。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-030",
    },
    {
        "chunk_id": "label_maturity_general",
        "document_id": "doc_guide_label",
        "document_type": "model_report",
        "title": "标签成熟度说明",
        "section_path": "评估规范/标签",
        "content": "逾期标签必须等待 T+30 成熟窗口，提前评估会低估模型表现。",
        "model_id": "",
        "case_id": "",
    },
    {
        "chunk_id": "score_distribution",
        "document_id": "doc_case_033",
        "document_type": "incident_report",
        "title": "评分分布漂移",
        "section_path": "调查过程/分布",
        "content": "评分分布整体右移但排序能力稳定，PSI 上升不一定意味着 KS 同步下降。",
        "model_id": "credit_v3",
        "case_id": "CASE-2026-033",
    },
    {
        "chunk_id": "unrelated_latency",
        "document_id": "doc_ops_001",
        "document_type": "operations_note",
        "title": "网关延迟排查",
        "section_path": "基础设施/网关",
        "content": "API 网关延迟来自连接池配置，与模型效果指标无关。",
        "model_id": "",
        "case_id": "",
    },
]

QUERIES = [
    ("credit_v3 最近 KS 从 0.42 下降到 0.31", "ks_label_maturity"),
    ("apply_cnt_30d PSI 漂移历史案例", "feature_psi_apply_cnt"),
    ("CASE-2026-018 调查过程", "feature_psi_apply_cnt"),
    ("T+30 标签成熟后性能恢复", "ks_label_maturity"),
    ("credit_v3 v3.2 版本异常", "model_v32_incident"),
    ("客群结构变化导致 KS 下降", "population_shift"),
    ("credit_v2 KS 下降案例", "credit_v2_ks_case"),
]


def _placeholder_vectors(count: int) -> list[list[float]]:
    vectors = []
    for index in range(count):
        vector = [0.0] * EMBEDDING_DIMENSION
        vector[index % EMBEDDING_DIMENSION] = 1.0
        vectors.append(vector)
    return vectors


def _embed(texts: list[str]) -> list[list[float]]:
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少 SILICONFLOW_API_KEY；设置后重试，或使用 --skip-dense 只验证 Milvus。"
        )
    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get(
            "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
        ),
    )
    response = client.embeddings.create(
        model=os.environ.get("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"),
        input=texts,
        encoding_format="float",
    )
    vectors = [item.embedding for item in response.data]
    dimensions = {len(vector) for vector in vectors}
    if dimensions != {EMBEDDING_DIMENSION}:
        raise ValueError(f"Embedding 维度不是预期的 1024：{sorted(dimensions)}")
    return vectors


def _create_collection(
    client: MilvusClient,
    collection_name: str,
    analyzer_params: dict[str, Any],
    *,
    include_dense: bool,
) -> None:
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    if include_dense:
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
        analyzer_params=analyzer_params,
    )
    if include_dense:
        schema.add_field("model_id", DataType.VARCHAR, max_length=128)
        schema.add_field("case_id", DataType.VARCHAR, max_length=128)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=EMBEDDING_DIMENSION)
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
    if include_dense:
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
        params={"inverted_index_algo": "DAAT_MAXSCORE", "bm25_k1": 1.2, "bm25_b": 0.75},
    )
    client.create_collection(collection_name, schema=schema, index_params=indexes)


def _rows(vectors: list[list[float]], version: str) -> list[dict[str, Any]]:
    rows = []
    for document, vector in zip(CORPUS, vectors, strict=True):
        rows.append({**document, "document_version": version, "dense_vector": vector})
    return rows


def _ids(search_result: Any) -> list[str]:
    return [hit["entity"]["chunk_id"] for hit in search_result[0]]


def _probe_bm25(
    client: MilvusClient, analyzer_name: str, analyzer_params: dict[str, Any]
) -> dict[str, Any]:
    collection = f"riskmind_stage0_{analyzer_name}"
    if client.has_collection(collection):
        client.drop_collection(collection)
    _create_collection(client, collection, analyzer_params, include_dense=False)
    client.insert(collection, [{"chunk_id": row["chunk_id"], "content": row["content"]} for row in CORPUS])
    client.flush(collection)
    client.load_collection(collection)
    results = []
    try:
        for query, expected in QUERIES:
            found = _ids(
                client.search(
                    collection,
                    data=[query],
                    anns_field="sparse_vector",
                    limit=3,
                    output_fields=["chunk_id"],
                    search_params={"metric_type": "BM25", "params": {}},
                )
            )
            results.append({"query": query, "expected": expected, "top3": found, "hit": expected in found})
    finally:
        client.drop_collection(collection)
    return {"hits_at_3": sum(item["hit"] for item in results), "total": len(results), "queries": results}


def run(skip_dense: bool, keep: bool) -> dict[str, Any]:
    client = MilvusClient(uri=os.environ.get("MILVUS_URI", "http://localhost:19530"))
    report: dict[str, Any] = {
        "runtime": {"milvus": client.get_server_version(), "pymilvus": __version__},
        "embedding": {
            "provider": "SiliconFlow",
            "model": os.environ.get("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"),
            "dimension": EMBEDDING_DIMENSION,
            "metric": "COSINE",
        },
    }

    report["analyzers"] = {
        name: [client.run_analyzer(text, params).tokens for text in ANALYZER_TEXTS]
        for name, params in ANALYZERS.items()
    }
    report["bm25"] = {
        name: _probe_bm25(client, name, params) for name, params in ANALYZERS.items()
    }

    for collection in (PHYSICAL_V1, PHYSICAL_V2):
        if client.has_collection(collection):
            client.drop_collection(collection)
    try:
        client.drop_alias(ACTIVE_ALIAS)
    except Exception:
        pass

    texts = [row["content"] for row in CORPUS]
    if skip_dense:
        vectors = _placeholder_vectors(len(texts))
        report["dense"] = {"status": "skipped", "reason": "--skip-dense"}
    else:
        vectors = _embed(texts)
        query_vectors = _embed([query for query, _ in QUERIES])
        report["dense"] = {"status": "validated", "queries": []}

    selected = ANALYZERS[SELECTED_ANALYZER]
    for collection, version in ((PHYSICAL_V1, "v001"), (PHYSICAL_V2, "v002")):
        _create_collection(client, collection, selected, include_dense=True)
        client.insert(collection, _rows(vectors, version))
        client.flush(collection)
        client.load_collection(collection)

    if not skip_dense:
        for (query, expected), vector in zip(QUERIES, query_vectors, strict=True):
            found = _ids(
                client.search(
                    PHYSICAL_V1,
                    data=[vector],
                    anns_field="dense_vector",
                    limit=3,
                    output_fields=["chunk_id"],
                    search_params={"metric_type": "COSINE", "params": {"ef": 64}},
                )
            )
            report["dense"]["queries"].append(
                {"query": query, "expected": expected, "top3": found, "hit": expected in found}
            )

    client.create_alias(PHYSICAL_V1, ACTIVE_ALIAS)
    before = client.describe_alias(ACTIVE_ALIAS)["collection_name"]
    client.alter_alias(PHYSICAL_V2, ACTIVE_ALIAS)
    after = client.describe_alias(ACTIVE_ALIAS)["collection_name"]
    alias_result = client.search(
        ACTIVE_ALIAS,
        data=["CASE-2026-018 apply_cnt_30d PSI"],
        anns_field="sparse_vector",
        limit=1,
        output_fields=["chunk_id", "document_version"],
        search_params={"metric_type": "BM25", "params": {}},
    )[0][0]["entity"]
    report["alias"] = {"before": before, "after": after, "search_after_switch": alias_result}
    filtered_result = client.search(
        PHYSICAL_V1,
        data=["KS 下降案例"],
        anns_field="sparse_vector",
        filter='model_id == "credit_v2"',
        limit=3,
        output_fields=["chunk_id", "model_id"],
        search_params={"metric_type": "BM25", "params": {}},
    )
    report["metadata_filter"] = [hit["entity"] for hit in filtered_result[0]]
    report["schema"] = {
        "physical_collections": [PHYSICAL_V1, PHYSICAL_V2],
        "active_alias": ACTIVE_ALIAS,
        "selected_analyzer": SELECTED_ANALYZER,
        "fields": [
            "chunk_id", "document_id", "document_type", "document_version",
            "title", "section_path", "content", "model_id", "case_id",
            "dense_vector", "sparse_vector",
        ],
    }

    if not keep:
        client.drop_alias(ACTIVE_ALIAS)
        client.drop_collection(PHYSICAL_V1)
        client.drop_collection(PHYSICAL_V2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-dense", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    report = run(skip_dense=args.skip_dense, keep=args.keep)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
