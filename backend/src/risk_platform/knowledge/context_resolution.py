"""将已检索的 Child 命中转换成最终阅读上下文，全程只查询 PostgreSQL。

Baseline：Child → Child 正文；Parent-Child：Child → Link → Parent 原文。
沿用检索阶段的路由快照，不重新搜索，也不在这里生成答案。
"""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.indexing.contracts import KnowledgeContextUnit
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeContextLinkRow, KnowledgeContextRow
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot


class ContextResolutionIntegrityError(ValueError):
    """权威 Child、Link 或 Parent 缺失/不一致；整次失败，不返回部分上下文。"""


class ContextResolutionResult(BaseModel):
    """解析结果：保留原请求的路由快照，并提供按命中顺序排列的上下文。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: RetrievalRouteSnapshot
    contexts: tuple[KnowledgeContext, ...]

# context_id：上下文 ID
# content：最终阅读的正文
# supporting_hits：哪些 Child 命中支持这份上下文    


async def _resolve(session: AsyncSession, result: DenseChildRetrievalResult) -> tuple[KnowledgeContext, ...]:
    """先复核 Child，再按 profile 选择直接返回正文或通过映射展开 Parent。"""

    # item 还包含相似度 score；本阶段只用 Child 的来源、正文和 rank，不聚合分数。
    hits = [item.hit for item in result.hits]
    ids = [hit.source.chunk_id for hit in hits]
    # ID 不得重复，排名必须按列表顺序恰好为 1、2、……、N。
    if len(set(ids)) != len(ids) or [hit.rank for hit in hits] != list(range(1, len(hits) + 1)):
        raise ContextResolutionIntegrityError("child hits must have unique IDs and contiguous ordered ranks")
    if not hits:
        return ()
    profile = result.snapshot.index_profile.value

    # 按原 snapshot 的 profile + Child ID 查 PG，不依赖数据库返回顺序。
    rows = list((await session.execute(select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == profile, KnowledgeChunkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())
    children = {row.chunk_id: row for row in rows}  # Child ID → PG Child 行。

# 文档 ID、文档版本
# 文档类型、model_id
# 标题、章节、正文
    if set(children) != set(ids):
        raise ContextResolutionIntegrityError("authoritative child is missing from the snapshot profile")
    for hit in hits:
        child = children[hit.source.chunk_id]
        # 即使输入来自检索阶段，也要确认传入的命中仍与 PG 权威身份和正文一致。
        if (hit.source.document_id, hit.source.document_version) != (child.document_id, child.document_version):
            raise ContextResolutionIntegrityError("child source identity differs from PostgreSQL")
        if any(getattr(hit, key) != getattr(child, key) for key in (
            "document_type", "model_id", "title", "section_path", "content",
        )):
            raise ContextResolutionIntegrityError("child hit payload differs from PostgreSQL")
        # model_fields 是契约声明的字段；构造字典并检查页码等 Child 数据规则。
        KnowledgeChunk.model_validate({key: getattr(child, key) for key in KnowledgeChunk.model_fields})


    if result.snapshot.index_profile == IndexProfile.BASELINE:
        # Baseline 不展开父块：一条 Child 对应一份 Context，支持来源只有它自己。
        return tuple(KnowledgeContext(context_id=hit.source.chunk_id, content=children[hit.source.chunk_id].content,
                                      supporting_hits=(hit,)) for hit in hits)
# 遍历每个命中 Child
#     ↓
# 创建一份 Context
#     ↓
# 使用 PG 中的 Child 正文
#     ↓
# supporting_hits 只放当前 Child

    # Parent-Child 第一步：找出每个命中 Child 的显式父块映射。
# 先读取这些 Child 的映射
    links = list((await session.execute(select(KnowledgeContextLinkRow).where(
        KnowledgeContextLinkRow.index_profile == profile, KnowledgeContextLinkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())
    by_child = {link.chunk_id: link for link in links}
    # 同时比较数量与 ID 集合，确保每个 Child 恰好有一条 Link，不能缺失或重复。
    if len(links) != len(ids) or set(by_child) != set(ids):
        raise ContextResolutionIntegrityError("every retrieved child requires exactly one parent link")
    context_ids = {link.context_id for link in links}  # 多个 Child 可能属于同一个 Parent，先去重查询。
    # 第二步：按同一 profile 批量取得 Parent 原文，不通过拼接 Child 还原 Parent。
    parents = list((await session.execute(select(KnowledgeContextRow).where(
        KnowledgeContextRow.index_profile == profile, KnowledgeContextRow.context_id.in_(context_ids),
    ).execution_options(populate_existing=True))).scalars())
    # 再从 PG 批量查询 Parent：Parent 正文直接来自 PG，不是用命中的 Child 临时拼出来的。
    by_parent = {parent.context_id: parent for parent in parents}
    if set(by_parent) != context_ids:
        raise ContextResolutionIntegrityError("当前路由快照对应的索引策略下缺少映射指向的父块")
    for parent in parents:
        KnowledgeContextUnit.model_validate({key: getattr(parent, key) for key in KnowledgeContextUnit.model_fields})
    grouped = {}  # Parent ID → 命中它的 Child 列表；字典保留 Parent 首次出现的顺序。
    ordinals = set()  # 记录 (Parent ID, 父内 Child 序号)，不同 Parent 可各自从 0 开始。
#Child Hit
#     ↓ 根据 chunk_id
# PG Child
#     ↓ 根据 chunk_id
# Link
#     ↓ 根据 context_id
# PG Parent
    for hit in hits:
        # 按原 Child rank 遍历，不能改用 Parent 查询顺序，也不按父内序号重新排序。
        child = children[hit.source.chunk_id]
        link = by_child[hit.source.chunk_id]
        parent = by_parent[link.context_id]
        ordinal_key = (parent.context_id, link.child_ordinal)
        # 这里只检查已命中 Child 的序号非负且不冲突；不要求检索命中全部兄弟块。
        if link.child_ordinal < 0 or ordinal_key in ordinals:
            raise ContextResolutionIntegrityError("parent links contain invalid child ordinals")
        ordinals.add(ordinal_key)
        # Child 与 Parent 必须来自同一文档版本、章节和来源，不能错连其他父块。
        if any(getattr(child, key) != getattr(parent, key) for key in (
            "document_id", "document_version", "document_type", "title", "section_path", "model_id", "source_uri",
        )):
            raise ContextResolutionIntegrityError("child and parent provenance differ")
        # 页码要么双方都没有，要么 Child 的整个页码范围落在 Parent 范围内。
        if (child.page_start is None) != (parent.page_start is None) or (
            child.page_start is not None and not (
                parent.page_start <= child.page_start <= child.page_end <= parent.page_end
            )
        ):
            raise ContextResolutionIntegrityError("子块与父块的页码信息不匹配，或子块页码范围超出父块")
        # 首次遇到 Parent 就创建列表，后续只追加支持 Child，Parent 不重复返回。
        grouped.setdefault(parent.context_id, []).append(hit)
    # Parent 排名由首次命中它的 Child 决定；正文直接读取 PG Parent，支持来源保留 Child。
    return tuple(KnowledgeContext(context_id=key, content=by_parent[key].content,
                                  supporting_hits=tuple(support)) for key, support in grouped.items())


async def resolve_knowledge_contexts(
    result: DenseChildRetrievalResult, *, session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ContextResolutionResult:
    """对外入口：沿用检索快照，将 Child 命中解析成 KnowledgeContext。

    不重新查路由、调用 Milvus/Embedding 或检查 READY。
    Parent 按最佳 Child rank 排列，不聚合分数，也不把 Parent 冒充 Child Hit。
    """
    # 重新经过数据契约校验，避免绕过校验构造或修改过的输入直接进入解析。
    result = DenseChildRetrievalResult.model_validate_json(result.model_dump_json())
    if not result.hits:  # 零命中直接返回，不必打开数据库会话。
        return ContextResolutionResult(snapshot=result.snapshot, contexts=())
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        contexts = await _resolve(session, result)
    return ContextResolutionResult(snapshot=result.snapshot, contexts=contexts)
