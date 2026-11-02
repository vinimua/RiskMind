"""按固定路由快照检索 Child，再用 PostgreSQL 权威数据检查每一条结果。

主线：请求 → 读取一次 serving route → 问题向量 → Milvus 搜索 → PG 校验 → Child Hit。
Milvus 负责找到相似内容，PG 负责证明这些内容的身份和载荷没有偏离权威数据。
本模块不查询 Parent、不生成最终 Context，也不生成答案。
"""

import math
from numbers import Real

from pydantic import BaseModel, ConfigDict, Field
from pymilvus import MilvusClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSearchRequest, KnowledgeSourceRef
from risk_platform.knowledge.embeddings import EMBEDDING_DIMENSION, EmbeddingClient, get_embedding_client
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.serving import resolve_serving_route
from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot


class ChildRetrievalIntegrityError(ValueError):
    """搜索结果、问题向量或来源数据不符合约定，整次请求不能作为可信结果返回。"""


class RetrievedKnowledgeHit(BaseModel):
    """一条已校验的 Child：hit 保存正文与来源，score 保存搜索相似度。"""

    # 拒绝未声明字段；禁止给模型字段重新赋值，减少后续流程误改结果的风险。
    model_config = ConfigDict(extra="forbid", frozen=True)
    hit: KnowledgeHit
    score: float = Field(allow_inf_nan=False)  # 拒绝 NaN/无穷值；不是相关性的概率。


class DenseChildRetrievalResult(BaseModel):
    """本次使用的路由快照，以及按 Milvus 原始排名保存的可信 Child。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: RetrievalRouteSnapshot  # profile、物理集合名、路由版本；后续解析继续携带它。
    hits: tuple[RetrievedKnowledgeHit, ...]  # 元组保存命中顺序，允许没有命中。


def _matches_filters(child: KnowledgeChunk, request: KnowledgeSearchRequest) -> bool:
    """在 PG 权威 Child 上复核筛选条件，不只相信 Milvus 执行过过滤。

    文档类型为空列表时不限制类型；其余字段为 None 时表示不限制该字段。
    所有已指定条件必须同时满足。
    """

    filters = request.filters
    return (
        (not filters.document_types or child.document_type in filters.document_types)
        # getattr(obj, key) 根据字段名读取属性；all() 要求每项都成立。
        and all(getattr(filters, key) is None or getattr(child, key) == getattr(filters, key)
                for key in ("document_id", "model_id", "document_version"))
    )


async def _authoritative_hits(
    session: AsyncSession, snapshot: RetrievalRouteSnapshot,
    request: KnowledgeSearchRequest, candidates: list[dict],
) -> tuple[RetrievedKnowledgeHit, ...]:
    """把 Milvus 候选转换成可信命中；任意候选不合法就整次失败。

    先按 snapshot.profile + chunk_id 批量查询 PG，再按候选顺序逐条校验。
    数据库返回顺序不作为排名依据，也不以 READY 判断 serving 候选资格。
    """

    # top_k 是最多返回几条，不保证一定凑满；返回超过上限则违反搜索契约。
    if len(candidates) > request.top_k:
        raise ChildRetrievalIntegrityError("dense search returned more than top_k children")
    ids = [candidate.get("chunk_id") for candidate in candidates]
    # ID 必须是非空字符串且不能重复；set() 去重后数量变少就说明有重复。
    if any(not isinstance(key, str) or not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ChildRetrievalIntegrityError("dense search returned invalid or duplicate child IDs")
    if not ids:  # 正常的零命中，直接返回空元组，不必查询 PG。
        return ()
    # 双重限定防止从其他 profile 找到同名 Child；不读取当前 active route。
    rows = list((await session.execute(select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == snapshot.index_profile.value,
        KnowledgeChunkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())  # 刷新会话中可能已缓存的 ORM 属性。
    by_id = {row.chunk_id: row for row in rows}  # ID → PG 行，后续无需依赖 SQL 返回顺序。
    # Milvus 中存在、PG 对应 profile 中不存在的 Child，不能作为可信来源返回。
    if set(by_id) != set(ids):
        raise ChildRetrievalIntegrityError("dense search child is missing from the authoritative profile")
    hits = []
    previous_score = math.inf  # 正无穷作为起点，第一条有限分数都可以通过顺序检查。
    for rank, candidate in enumerate(candidates, 1):
        # enumerate(..., 1) 生成 (1, 首条候选)、(2, 次条候选)……，rank 从 1 开始。
        score = candidate.get("score")
        # 排除布尔值、非实数、NaN 和正负无穷；当前没有增加 COSINE [-1, 1] 范围检查。
        if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score):
            raise ChildRetrievalIntegrityError("dense search returned a non-finite or invalid score")
        if score > previous_score:  # 后一条不得比前一条更高；同分允许，不重新排序。
            raise ChildRetrievalIntegrityError("cosine hits must be ordered by descending score")
        previous_score = score
        row = by_id[candidate["chunk_id"]]
        # 以 PG 字段为标准：正文、标题、章节、文档身份、来源 URI、页码等逐项比对。
        # OUTPUT_FIELDS 不含 dense_vector，因此这里没有重新计算或比较向量本身。
        payload = {field: getattr(row, field) for field in milvus.OUTPUT_FIELDS}
        if any(field not in candidate or candidate[field] != value for field, value in payload.items()):
            raise ChildRetrievalIntegrityError("Milvus child payload differs from authoritative PostgreSQL data")
        child = KnowledgeChunk.model_validate(payload)  # 再检查 Child 数据契约，如来源页码规则。
        if not _matches_filters(child, request):
            raise ChildRetrievalIntegrityError("authoritative child does not match requested filters")
        # 返回对象由 PG 权威载荷构造；Milvus 只提供候选身份、顺序和分数。
        hits.append(RetrievedKnowledgeHit(
            hit=KnowledgeHit(
                source=KnowledgeSourceRef(document_id=child.document_id,
                                          document_version=child.document_version, chunk_id=child.chunk_id),
                document_type=child.document_type, model_id=child.model_id, title=child.title,
                section_path=child.section_path, content=child.content, rank=rank,
            ), score=float(score),
        ))
    return tuple(hits)  # 保留原始 Child rank；Parent 去重属于 context_resolution.py。


async def retrieve_dense_children(
    request: KnowledgeSearchRequest, *, index_profile: IndexProfile,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None, milvus_client: MilvusClient | None = None,
) -> DenseChildRetrievalResult:
    """完成一次 Dense Child 检索，只返回与 PG 数据一致的命中。

    路由只解析一次；中途上线新版本，也继续搜索本次快照中的旧物理集合。
    缺路由、依赖失败或数据不一致时明确报错，不回退 alias、不返回部分成功结果。
    """
    # 序列化后重新校验，确保传入的请求重新经过 Pydantic 的字段和业务规则检查。
    request = KnowledgeSearchRequest.model_validate_json(request.model_dump_json())
    profile = IndexProfile(index_profile)
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        snapshot = await resolve_serving_route(session, profile)
    # 取到快照即结束此次数据库会话，不在等待 Embedding 时一直保持这份会话。
    embedder = embedding_client or get_embedding_client()
    vector = await embedder.embed_query(request.query)
    # 检索入口再次校验向量，注入自定义客户端时也不能绕过这些要求。
    # not any(vector) 表示所有分量都是 0；零向量不适合当前 COSINE 搜索。
    if len(vector) != EMBEDDING_DIMENSION or any(
        isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) for value in vector
    ) or not any(vector):
        raise ChildRetrievalIntegrityError("query embedding must be a finite, nonzero 1024-dimensional vector")

    def search():
        """在线程内使用快照中的物理集合执行同步 Milvus API。"""

        client = milvus_client or milvus.get_client()
        if not client.has_collection(snapshot.collection):
            raise ChildRetrievalIntegrityError("serving physical collection is missing")
        # 检索只检查现有集合，不替丢失的 serving 集合创建一个空集合。
        milvus.validate_collection_for_profile(client, snapshot.collection, snapshot.index_profile)
        return milvus.dense_search(client, vector, request.top_k, filters=request.filters,
                                   collection=snapshot.collection)

    candidates = await milvus.acall(search)  # 同步网络调用交给专用线程，避免阻塞事件循环。
    async with sessions() as session:
        # 搜索完成后再开 PG 会话复核；这里传原 snapshot，不重新查询 active route。
        hits = await _authoritative_hits(session, snapshot, request, candidates)
    return DenseChildRetrievalResult(snapshot=snapshot, hits=hits)
