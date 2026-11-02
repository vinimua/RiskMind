"""基于固定 gold 标注计算召回排序指标，不改变检索行为。"""

import math

from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile


def relevant_child_ids(question, documents, profile: IndexProfile) -> frozenset[str]:
    """从完整固定语料派生当前 profile 的相关 Child 集合。

    相关定义为来源一致且包含至少一个完整 gold quote，不是 LLM 语义判断。
    标注不穷尽时，未标注但实际相关的 Child 仍可能被视为不相关。
    """
    strategy = BaselineIndexBuildStrategy() if profile == IndexProfile.BASELINE else ParentChildIndexBuildStrategy()
    return frozenset(
        chunk.chunk_id
        for doc in documents
        for chunk in strategy.build(doc).search_chunks
        if any((chunk.document_id, chunk.document_version) == (quote.document_id, quote.document_version)
               and quote.text in chunk.content for quote in question.relevant_quotes)
    )


def measure_retrieval(retrieved, *, relevant_ids: frozenset[str], top_k: int) -> dict:
    """二元相关性：Precision 分母为请求 k，Recall 分母为完整语料的相关 Child 数。"""
    if type(top_k) is not int or top_k <= 0:
        raise ValueError("top_k 必须为正整数")
    ids = [item.hit.source.chunk_id for item in retrieved.hits]
    if len(ids) > top_k or len(ids) != len(set(ids)):
        raise ValueError("检索结果不能超过 top_k 或包含重复 Child")
    if [item.hit.rank for item in retrieved.hits] != list(range(1, len(ids) + 1)):
        raise ValueError("检索排名必须从 1 开始连续排列")
    relevance = [key in relevant_ids for key in ids]
    matched = sum(relevance)
    dcg = sum(1 / math.log2(rank + 1) for rank, relevant in enumerate(relevance, 1) if relevant)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(top_k, len(relevant_ids)) + 1))
    return {
        "retrieval_precision_at_k": matched / top_k,
        "retrieval_recall_at_k": matched / len(relevant_ids) if relevant_ids else None,
        "retrieval_hit_at_k": bool(matched),
        "retrieval_mrr_at_k": next((1 / rank for rank, relevant in enumerate(relevance, 1) if relevant), 0.0),
        "retrieval_ndcg_at_k": dcg / ideal if ideal else None,
        "retrieval_relevant_child_count": len(relevant_ids),
        "retrieval_matched_child_count": matched,
        "retrieval_k": top_k,
        "retrieval_label_basis": "same-source-verbatim-gold-quote-v1",
    }
