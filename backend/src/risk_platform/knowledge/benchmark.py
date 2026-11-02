"""离线对比 Baseline 与 Parent-Child，使用同一份固定语料和问题集。

标准引用按“同来源、完整原文包含”衡量召回与上下文覆盖，不直接证明答案正确。
可选生成评审交给 generation_evaluator；本模块不入库、不重建、不切换线上路由。
"""

import hashlib
import json
from time import perf_counter
from typing import Callable, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.chunking import KnowledgeDocument
from risk_platform.knowledge.context_resolution import resolve_knowledge_contexts
from risk_platform.knowledge.contracts import KnowledgeSearchRequest
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import retrieve_dense_children
from risk_platform.knowledge.retrieval_metrics import measure_retrieval, relevant_child_ids


async def verify_benchmark_corpus(dataset, sessions):
    """确认两个策略的权威 Child 和 serving 验收都对应这份固定评测语料。

    应使用专用评测数据库/集合，不能把旧快照或混入其他文档的集合当作公平对比。
    这里只读取 PG 及验收记录，不重新逐条读取 Milvus 全集合进行验收。
    """
    from sqlalchemy import select
    from risk_platform.knowledge import milvus
    from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
    from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeCollectionBuildRow, KnowledgeDocumentRow
    from risk_platform.knowledge.serving import authoritative_snapshot, resolve_serving_route

    snapshots = {}
    async with sessions() as session:
        for doc in dataset.documents:
            # 文档 ID+版本相同还不够，PG 保存的完整 canonical 文档也必须与数据集相同。
            row = await session.get(KnowledgeDocumentRow, (doc.document_id, doc.document_version))
            if row is None or KnowledgeDocument.model_validate_json(row.source_json) != doc:
                raise ValueError("benchmark canonical source differs from dataset")
        for profile in IndexProfile:
            # 在内存重跑纯构建策略，推导该语料应有的 Child；不把结果重新写入数据库。
            strategy = BaselineIndexBuildStrategy() if profile == IndexProfile.BASELINE else ParentChildIndexBuildStrategy()
            expected = {chunk.chunk_id: chunk for doc in dataset.documents for chunk in strategy.build(doc).search_chunks}
            rows = list((await session.execute(select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.index_profile == profile.value,
            ))).scalars())
            # 查该 profile 的全部 PG Child，检查缺失、多余和业务字段变化。
            if len(rows) != len(expected) or any(
                row.chunk_id not in expected or any(getattr(row, key) != getattr(expected[row.chunk_id], key)
                                                  for key in milvus.OUTPUT_FIELDS) for row in rows
            ):
                raise ValueError("benchmark profile children differ from the fixed corpus")
            snapshot = await resolve_serving_route(session, profile)
            snapshots[profile.value] = snapshot
            build = (await session.execute(select(KnowledgeCollectionBuildRow).where(
                KnowledgeCollectionBuildRow.collection == snapshot.collection,
                KnowledgeCollectionBuildRow.index_profile == profile.value,
            ))).scalar_one()
            # 当前权威来源的数量与摘要，必须等于当前 serving 集合验收时记录的来源。
            if await authoritative_snapshot(session, profile) != (build.source_chunk_count, build.authoritative_snapshot_digest):
                raise ValueError("benchmark serving acceptance does not cover the current corpus")
    return snapshots


class BenchmarkContract(BaseModel):
    """评测数据的共同约束：拒绝未知字段，禁止直接给模型字段重新赋值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldQuote(BenchmarkContract):
    """人工标注的标准原文：通过文档 ID+版本定位来源，通过 text 检查完整覆盖。"""

    quote_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    text: str = Field(min_length=1)


class BenchmarkQuestion(BenchmarkContract):
    """一个问题的检索请求、标准引用，以及生成侧可选的参考答案。"""

    question_id: str = Field(min_length=1)
    request: KnowledgeSearchRequest
    relevant_quotes: tuple[GoldQuote, ...] = Field(min_length=1)
    reference_answer: str | None = Field(default=None, min_length=1)


class BenchmarkDataset(BenchmarkContract):
    """固定语料与问题集；annotation_notes 记录标注方法与已知限制。"""

    dataset_id: str = Field(min_length=1)
    annotation_notes: str = Field(min_length=1)
    documents: tuple[KnowledgeDocument, ...] = Field(min_length=1)
    questions: tuple[BenchmarkQuestion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_gold(self) -> Self:
        """字段校验后检查去重及引用来源，避免错误标注让指标失真。"""

        # 复合键防止同一文档版本在语料中出现两次。
        documents = {(doc.document_id, doc.document_version): doc for doc in self.documents}
        if len(documents) != len(self.documents):
            raise ValueError("duplicate corpus source")
        if len({q.question_id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question ID")
        for question in self.questions:
            quotes = question.relevant_quotes
            if len({q.quote_id for q in quotes}) != len(quotes):
                raise ValueError("duplicate gold quote ID")
            # 不只检查 quote_id；同一来源的同一段原文换个 ID 也不能重复计分。
            identities = {(q.document_id, q.document_version, q.text) for q in quotes}
            if len(identities) != len(quotes):
                raise ValueError("duplicate gold quote")
            for quote in quotes:
                doc = documents.get((quote.document_id, quote.document_version))
                # 要求原文完整出现在某一页；不做模糊匹配，不把跨页片段自动拼成引用。
                if doc is None or not quote.text.strip() or not any(quote.text in p.content for p in doc.pages):
                    raise ValueError("gold quote must occur verbatim in its corpus source")
        return self


def dataset_digest(dataset: BenchmarkDataset) -> str:
    """固定 JSON 格式后计算 SHA-256，标识评测使用的具体数据集内容。"""

    data = dataset.model_dump(mode="json")
    # 新增可选参考答案时，不改变旧 v1 数据集的既有摘要。
    for question in data["questions"]:
        if question["reference_answer"] is None:
            question.pop("reference_answer")
    # 字典键排序、去格式空格、保留中文；列表顺序仍保留，顺序变化会影响摘要。
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def measure_contexts(question, retrieved, resolved, *, count_tokens: Callable[[str], int] | None = None):
    """比较 Child 与最终 Context 对标准引用的覆盖，以及上下文长度与冗余。

    必须包含完整引用才算覆盖；coverage 是完整覆盖引用的字符长度加权比例。
    context_precision 是“含标准引用的 Context 条数比例”，不是逐 token 精确率。
    """
    # 检索和解析必须属于同一次路由快照，不能拼接两个线上版本的结果。
    if retrieved.snapshot != resolved.snapshot:
        raise ValueError("context snapshot differs from retrieval")
    expected = {item.hit.source.chunk_id: item.hit for item in retrieved.hits}
    support = [hit for context in resolved.contexts for hit in context.supporting_hits]
    # 所有 Context 的 supporting_hits 必须恰好分配全部检索 Child：不丢失、不重复。
    if len(support) != len(expected) or {hit.source.chunk_id for hit in support} != set(expected):
        raise ValueError("contexts must partition retrieved children")
    if any(hit != expected[hit.source.chunk_id] for hit in support):
        raise ValueError("supporting child payload differs from retrieval")

    def matches(text, hits):
        """正文包含完整 quote，且支持 Child 来自该 quote 的文档版本，才算命中。"""

        sources = {(hit.source.document_id, hit.source.document_version) for hit in hits}
        return {q.quote_id for q in question.relevant_quotes
                if (q.document_id, q.document_version) in sources and q.text in text}

    # 每个元素是该 Child/Context 覆盖的 quote_id 集合；union 汇总去重后的覆盖。
    child_sets = [matches(item.hit.content, (item.hit,)) for item in retrieved.hits]
    context_sets = [matches(context.content, context.supporting_hits) for context in resolved.contexts]
    children = set().union(*child_sets)
    contexts = set().union(*context_sets)
    quote_matches = sum(len(ids) for ids in context_sets)  # 同一引用被不同 Context 覆盖会累计多次。
    child_chars = sum(len(item.hit.content) for item in retrieved.hits)
    context_chars = sum(len(context.content) for context in resolved.contexts)
    counts = None
    if count_tokens is not None:
        # 只统计 Context 正文 token；不包括问题、标题、提示词和输出答案。
        counts = [count_tokens(context.content) for context in resolved.contexts]
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("token counter must return nonnegative integers")
    return {
        # 按 quote 条数计算，分别观察小块召回和 Parent 展开后的覆盖收益。
        "child_relevant_quote_recall": len(children) / len(question.relevant_quotes),
        "context_relevant_quote_recall": len(contexts) / len(question.relevant_quotes),
        "context_precision": sum(bool(ids) for ids in context_sets) / len(context_sets) if context_sets else 0.0,
        # 长引用权重更大，但只要未完整包含就不贡献分子，不按部分匹配给分。
        "context_coverage": sum(len(q.text) for q in question.relevant_quotes if q.quote_id in contexts)
                            / sum(len(q.text) for q in question.relevant_quotes),
        "complete_relevant_context": len(contexts) == len(question.relevant_quotes),
        "context_tokens": sum(counts) if counts is not None else None,  # 未提供 tokenizer 时保持未知，不按字符猜测。
        "context_characters": context_chars,
        "context_to_child_character_ratio": context_chars / child_chars if child_chars else None,
        # 衡量标准引用重复命中，不是任意正文重复率。
        "redundant_gold_match_ratio": (quote_matches - len(contexts)) / quote_matches if quote_matches else 0.0,
        "child_count": len(retrieved.hits), "context_count": len(resolved.contexts),
        "covered_quote_ids": sorted(contexts),
        "missing_quote_ids": sorted(q.quote_id for q in question.relevant_quotes if q.quote_id not in contexts),
    }


async def run_benchmark(dataset: BenchmarkDataset, *, repeats=3, count_tokens=None,
                        generation_evaluator=None, **pipeline_options):
    """对每个问题成对运行两种策略，并保留原始命中、上下文及路由用于复核。

    检索耗时包含 Embedding、Milvus 和 PG，解析耗时单独记录；指标/token 计算不计入。
    检索或完整性异常直接向上传播；可选生成评审未全部成功时总结果为 INCONCLUSIVE。
    """
    dataset = BenchmarkDataset.model_validate_json(dataset.model_dump_json())
    if type(repeats) is not int or repeats < 1:  # 严格拒绝 bool；不能把 True 当作重复一次。
        raise ValueError("repeats must be positive")
    if generation_evaluator is not None and any(
        not question.reference_answer or not question.reference_answer.strip() for question in dataset.questions
    ):
        raise ValueError("生成侧评测要求每个问题提供非空参考答案")
    from risk_platform.db.database import get_session_factory
    sessions = pipeline_options.get("session_factory") or get_session_factory()
    snapshots = await verify_benchmark_corpus(dataset, sessions)  # 先记录两个 profile 的基准路由。
    # 每个问题、每个 profile 各有自己的相关 Child ID 标签，因为两套策略的 Child 不同。
    labels = {(question.question_id, profile): relevant_child_ids(question, dataset.documents, profile)
              for question in dataset.questions for profile in IndexProfile}
    rows = []
    for repeat in range(repeats):
        for index, question in enumerate(dataset.questions):
            profiles = list(IndexProfile)
            # 相邻问题/重复轮次交替先跑哪种策略，减轻固定先后顺序的缓存/预热偏差。
            # 这不是完全随机实验，也不消除网络或上游模型波动。
            if (repeat + index) % 2:
                profiles.reverse()
            for profile in profiles:
                start = perf_counter()  # 单调高精度计时器，用于测耗时而非记录日历时间。
                retrieved = await retrieve_dense_children(question.request, index_profile=profile, **pipeline_options)
                after_search = perf_counter()
                resolved = await resolve_knowledge_contexts(retrieved, session_factory=pipeline_options.get("session_factory"))
                end = perf_counter()
                # 单次请求虽可安全继续用旧 snapshot，但整组评测不允许混入不同路由版本。
                previous = snapshots.setdefault(profile.value, retrieved.snapshot)
                if previous != retrieved.snapshot:
                    raise ValueError("serving route changed during benchmark; rerun on stable routes")
                metrics = measure_contexts(question, retrieved, resolved, count_tokens=count_tokens)
                # 合并独立模块的 Child Precision/Recall/MRR/nDCG 等指标。
                metrics.update(measure_retrieval(retrieved, relevant_ids=labels[question.question_id, profile],
                                                top_k=question.request.top_k))
                row = {
                    "question_id": question.question_id, "repeat": repeat, "profile": profile.value,
                    "retrieval_ms": (after_search - start) * 1000,
                    "resolution_ms": (end - after_search) * 1000,
                    "total_ms": (end - start) * 1000,
                    "metrics": metrics,
                    "retrieved": retrieved.model_dump(mode="json"), "resolved": resolved.model_dump(mode="json"),
                }
                if generation_evaluator is not None:
                    # 可选：用实际 Context 生成答案，再交给结构化 LLM 评审器；不改检索结果。
                    row["generation"] = await generation_evaluator.evaluate(question, resolved)
                    if row["generation"].get("generation_ms") is not None:
                        row["answer_pipeline_ms"] = row["total_ms"] + row["generation"]["generation_ms"]  # 不含 judge 耗时。
                rows.append(row)
    # 结束时再核验语料与路由；这是前后检查，不是整个评测期间持续锁定数据库。
    if snapshots != await verify_benchmark_corpus(dataset, sessions):
        raise ValueError("serving route changed during benchmark; rerun on stable routes")
    generation_ok = all(row.get("generation", {}).get("status") == "SUCCEEDED" for row in rows)
    # 返回逐次原始记录；跨问题/重复次数的汇总报告由 scripts/benchmark_knowledge.py 处理。
    return {"status": "SUCCEEDED" if generation_evaluator is None or generation_ok else "INCONCLUSIVE",
            "retrieval_status": "SUCCEEDED",
            "generation_status": "NOT_RUN" if generation_evaluator is None else (
                "SUCCEEDED" if generation_ok else "INCONCLUSIVE"), "dataset_id": dataset.dataset_id,
            "dataset_digest": dataset_digest(dataset), "repeats": repeats, "rows": rows}
