"""知识数据的 PostgreSQL 读写与一致性检查，不执行 Embedding 或 Milvus 写入。

主线：stage_prepared() 检查文档与准备结果，再写入 Document / Build / Child / Parent / Link。
本模块不 commit，事务边界由调用方负责；flush 发送 SQL，但不代表事务已经提交。
所有新增数据应放在调用方的同一事务内，异常时由该事务回滚，避免只保存部分构建结果。
"""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.chunking import KnowledgeDocument, chunk_document
from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy, PreparedKnowledge
from risk_platform.knowledge.models import (
    KnowledgeBuildRow, KnowledgeCollectionRow, KnowledgeContextLinkRow,
    KnowledgeContextRow, KnowledgeDocumentRow,
)


class DocumentVersionConflictError(ValueError):
    """同一不可变文档版本或构建身份被提交了不同内容，调用方应使用新版本。"""


async def lock_profile_source(
    session: AsyncSession, profile: IndexProfile, *, shared: bool = False,
) -> None:
    """为某个 profile 的权威 Child 集合加事务级 advisory lock。

    staging 使用共享锁，允许不同文档并发写入；serving 验收/激活使用独占锁，
    暂时阻止来源集合变化。该锁是协作约定，不会阻止不遵守约定的人工 SQL。
    """

    # 函数名只能从这两个固定值选择；profile 对应的锁键通过绑定参数传入。
    function = "pg_advisory_xact_lock_shared" if shared else "pg_advisory_xact_lock"
    await session.execute(
        text(f"SELECT {function}(hashtextextended(:key, 0))"),
        {"key": f"riskmind:knowledge-source:{profile.value}"},
    )


def _canonical(value: BaseModel) -> str:
    """转成固定格式 JSON，供原文比较和构建摘要使用。

    mode="json" 将枚举等转换成 JSON 值；字典键排序、去多余格式空格，保留中文。
    列表顺序和字符串内容不会被重排或规范化，因此片段顺序变化仍会影响摘要。
    """

    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def stage_prepared(
    session: AsyncSession,
    document: KnowledgeDocument,
    prepared: PreparedKnowledge,
    *,
    index_profile: IndexProfile,
) -> int:
    """在调用方事务中保存一份文档的完整构建结果，返回新增 Child 数量。

    完全一致的重复提交返回 0；同版本原文或同身份构建不同则失败，不覆盖旧数据。
    文档锁与旧 stage_chunks 入口共用且不含 profile，防止不同策略改写同一文档版本。
    这里只 stage 到 PG，不代表 Child 已写入 Milvus，也不切换 serving route。
使用哪个数据库会话
原始文档是什么
构建出了哪些 Child、Parent、Link
这些结果属于哪个构建方案
    """

    profile = IndexProfile(index_profile)  # 限定当前支持的策略，非法值立即失败。
    # 序列化后重新校验并复制输入，不仅依赖调用方已经创建过模型这一事实。
    document = KnowledgeDocument.model_validate_json(document.model_dump_json())
    prepared = PreparedKnowledge.model_validate_json(prepared.model_dump_json())
    expected = (  # 用该 profile 的固定策略复算，防止拿其他规则的结果冒充此 profile。
        BaselineIndexBuildStrategy().build(document) if profile == IndexProfile.BASELINE
        else ParentChildIndexBuildStrategy().build(document)
    )
    if prepared != expected:
        raise ValueError("prepared knowledge does not match the frozen index profile")
    if not prepared.search_chunks:
        raise ValueError("at least one knowledge chunk is required")
    key = (document.document_id, document.document_version)  # canonical 文档的联合主键。
    # 先锁定文档 ID + 版本，再查旧数据，避免并发请求都误以为是首次插入。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"riskmind:knowledge:{key[0]}:{key[1]}"},
    )
    source_json = _canonical(document)  # 对比完整输入文档，而不只是比较 Child 正文。
    source = await session.get(KnowledgeDocumentRow, key)
    if source is not None and source.source_json != source_json:
        raise DocumentVersionConflictError("document version is immutable across index profiles")
    if source is None:
        # 兼容早期只有 Baseline Child、没有 canonical 文档记录的数据。
        # 新提供的文档必须能重现全部旧 Child 业务字段，才允许补登记原文。
        legacy = list((await session.execute(select(KnowledgeChunkRow).where(
            KnowledgeChunkRow.document_id == key[0],
            KnowledgeChunkRow.document_version == key[1],
            KnowledgeChunkRow.index_profile == IndexProfile.BASELINE.value,
        ))).scalars())
        if legacy and sorted(_row_business_values(row) for row in legacy) != sorted(
            _chunk_business_values(chunk) for chunk in chunk_document(document)
        ):
            raise DocumentVersionConflictError("legacy document version is immutable")
        session.add(KnowledgeDocumentRow(
            document_id=key[0], document_version=key[1], source_json=source_json,
        ))
        await session.flush()  # 先写出文档，使后续 Build 外键有目标；仍未提交。

    digest = hashlib.sha256(_canonical(prepared).encode("utf-8")).hexdigest()  # 覆盖 Child、Parent、Link 及其顺序。
    existing = await session.get(KnowledgeBuildRow, (profile.value, *key))
    if existing is not None:
        if existing.prepared_digest != digest:
            raise DocumentVersionConflictError("index build is immutable; use a new profile or document version")
        return 0  # 构建指纹相同，重复 staging 不重写记录，也不重置 READY。
    
    session.add(KnowledgeBuildRow(
        index_profile=profile.value, document_id=key[0], document_version=key[1],
        prepared_digest=digest,
    ))
    await session.flush()  # Build 先于 Parent 插入，满足 Parent 指向 Build 的复合外键。
    count = await _stage_chunks(session, prepared.search_chunks, index_profile=profile)
    # 新 Child 以 PENDING 保存；Parent 保存原文上下文，不进行向量化。
    session.add_all([
        KnowledgeContextRow(index_profile=profile.value, **unit.model_dump())
        for unit in prepared.context_units
    ])
    await session.flush()  # Link 插入前确保其 Parent 目标已写入当前事务。
    session.add_all([
        KnowledgeContextLinkRow(index_profile=profile.value, **link.model_dump())
        for link in prepared.context_links
    ])
    await session.flush()  # 检查 Link 的外键和唯一约束，但最终提交仍由调用方完成。
    return count  # 这里统计的是新增 Child，不是 Document、Parent 或 Link 总数。


async def bind_collection(
    session: AsyncSession, *, collection: str, index_profile: IndexProfile, purpose: str,
) -> None:
    """登记物理 collection 的 profile 与用途，每个 profile 至多一个 INGESTION 目标。

    REBUILD 不会自动成为 INGESTION 或 serving 目标；本函数只登记 PG 归属。
    Milvus 兼容性预检由 ingestion/rebuild 调用方在绑定前执行，避免明显冲突先占用目标。
    """
    profile = IndexProfile(index_profile)
    if purpose not in {"INGESTION", "REBUILD"}:
        raise ValueError("invalid collection purpose")
    # 统一先锁 profile、再锁 collection；同目标的竞争请求通过 collection 锁串行化。
    for key in (f"profile:{profile.value}", f"collection:{collection}"):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"riskmind:knowledge-index:{key}"},
        )
    existing = await session.get(KnowledgeCollectionRow, collection)
    if existing is not None:
        if (existing.index_profile, existing.purpose) != (profile.value, purpose):
            raise ValueError("physical collection is already bound to another profile or purpose")
        return  # 同名、同 profile、同用途是幂等绑定；不能借重复调用更换 owner。
    if purpose == "INGESTION":
        # REBUILD 可有多个，但增量写入目标不能随意更换；数据库部分唯一索引也会兜底。
        target = (await session.execute(select(KnowledgeCollectionRow).where(
            KnowledgeCollectionRow.index_profile == profile.value,
            KnowledgeCollectionRow.purpose == purpose,
        ))).scalar_one_or_none()
        if target is not None:
            raise ValueError("profile already has an ingestion target; use rebuild for a new collection")
    session.add(KnowledgeCollectionRow(
        collection=collection, index_profile=profile.value, purpose=purpose,
    ))
    await session.flush()


def _chunk_business_values(chunk: KnowledgeChunk) -> tuple[object, ...]:
    """提取输入 Child 的业务字段，供新旧内容比较；不包含可变状态和时间戳。

    文档 ID、版本和 profile 由调用方限定，枚举类型转为数据库保存的字符串值。
    字段顺序必须与 _row_business_values() 一致。
    """

    return (
        chunk.chunk_id,
        chunk.document_type.value,
        chunk.title,
        chunk.section_path,
        chunk.content,
        chunk.model_id,
        chunk.source_uri,
        chunk.page_start,
        chunk.page_end,
    )


def _row_business_values(row: KnowledgeChunkRow) -> tuple[object, ...]:
    """从 ORM 行提取同样顺序的业务字段，使数据库记录能与 KnowledgeChunk 对照。"""

    return (
        row.chunk_id,
        row.document_type,
        row.title,
        row.section_path,
        row.content,
        row.model_id,
        row.source_uri,
        row.page_start,
        row.page_end,
    )


async def stage_chunks(
    session: AsyncSession,
    chunks: Sequence[KnowledgeChunk],
) -> int:
    """兼容早期调用的 Baseline-only 入口；完整策略结果应使用 stage_prepared()。

    此入口不会自行创建 Parent、Link 或完整 Build，不能用它保存 Parent-Child 产物。
    """
    if any(chunk.chunk_id.startswith("pc-child-") for chunk in chunks):
        raise ValueError("Parent-Child output must use stage_prepared with its index profile")
    return await _stage_chunks(session, chunks, index_profile=IndexProfile.BASELINE)


async def _stage_chunks(
    session: AsyncSession,
    chunks: Sequence[KnowledgeChunk],
    *,
    index_profile: IndexProfile,
) -> int:
    """保存一个 profile 下的一份不可变文档版本的 Child，不提交事务。

    返回新增行数；数据库已有相同业务内容时返回 0，任何业务字段不同则冲突。
    相同重试不重置已有入库状态；新行统一为 PENDING。
    """

    if not chunks:
        raise ValueError("at least one knowledge chunk is required")

    document_keys = {
        (chunk.document_id, chunk.document_version) for chunk in chunks
    }
    if len(document_keys) != 1:  # 一次只能提交一份文档的一个版本。
        raise ValueError("all chunks must belong to one document version")
    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):  # 相同批次不能出现重复片段 ID。
        raise ValueError("chunk_id values must be unique within a document version")

    document_id, document_version = next(iter(document_keys))  # 集合此时恰好只有一个文档身份。
    # 与 stage_prepared 使用同一文档锁；锁在调用方事务 commit/rollback 时释放。
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"riskmind:knowledge:{document_id}:{document_version}"},
    )
    # 同 profile 的 staging 共享此锁；serving 验收/激活持独占形式，确保校验期间 Child 集合稳定。
    await lock_profile_source(session, index_profile, shared=True)
    if index_profile == IndexProfile.BASELINE:
        # 已有 canonical 文档时，旧 Baseline 入口也必须与该原文的固定切分结果一致。
        source = await session.get(KnowledgeDocumentRow, (document_id, document_version))
        if source is not None:
            expected = chunk_document(KnowledgeDocument.model_validate_json(source.source_json))
            if sorted(_chunk_business_values(c) for c in chunks) != sorted(
                _chunk_business_values(c) for c in expected
            ):
                raise DocumentVersionConflictError("document version is immutable across index profiles")
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.document_id == document_id,
            KnowledgeChunkRow.document_version == document_version,
            KnowledgeChunkRow.index_profile == index_profile.value,
        )
        .order_by(KnowledgeChunkRow.chunk_id)
    )
    existing = list(result.scalars())  # scalars() 取得 ORM 对象，而不是包含对象的 SQL Row 包装。
    if existing:
        # 按稳定字段排序比较完整集合，忽略调用方列表顺序，但不忽略业务内容。
        # stage_prepared 上层另用 prepared_digest 保护整个准备结果的顺序。
        incoming_values = sorted(_chunk_business_values(chunk) for chunk in chunks)
        existing_values = sorted(_row_business_values(row) for row in existing)
        if incoming_values == existing_values:
            return 0  # 已存在相同数据，无需覆盖、重复插入或退回 PENDING。
        raise DocumentVersionConflictError(
            f"document version {document_id}/{document_version} is immutable; "
            "use a new document_version"
        )

    session.add_all(
        [
            KnowledgeChunkRow(
                index_profile=index_profile.value,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                document_version=chunk.document_version,
                document_type=chunk.document_type.value,
                title=chunk.title,
                section_path=chunk.section_path,
                content=chunk.content,
                model_id=chunk.model_id,
                source_uri=chunk.source_uri,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                ingestion_status=IngestionStatus.PENDING.value,  # PG staging 成功不表示向量副本已写完。
            )
            for chunk in chunks
        ]
    )
    await session.flush()  # 执行 INSERT 并检查约束，不在仓储内部 commit。
    return len(chunks)  # 此分支没有旧记录，因此全部输入 Child 都是新增行。


async def list_pending_chunks(
    session: AsyncSession,
    *,
    limit: int,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> list[KnowledgeChunkRow]:
    """按 profile 取得一批 PENDING Child，以 chunk_id 排序保证批次顺序确定。

    这里只读取，不抢占任务、不加行锁，也不修改状态；并发与写入协调由上层负责。
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    result = await session.execute(
        select(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
            KnowledgeChunkRow.ingestion_status
            == IngestionStatus.PENDING.value
        )
        .order_by(KnowledgeChunkRow.chunk_id)
        .limit(limit)
    )
    return list(result.scalars())


async def list_rebuild_chunks(
    session: AsyncSession,
    *,
    limit: int,
    after_chunk_id: str | None = None,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> list[KnowledgeChunkRow]:
    """按游标遍历该 profile 的全部权威 Child，供 rebuild 使用，不改变原入库状态。

    PENDING 和 READY 都包括；after_chunk_id 使用严格大于条件，避免重复读取上一页尾行。
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    statement = select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
        KnowledgeChunkRow.ingestion_status.in_(
            (IngestionStatus.PENDING.value, IngestionStatus.READY.value)
        )
    )
    if after_chunk_id is not None:
        # 这是 keyset 分页，不使用 OFFSET；调用方传上一批最后一个 chunk_id。
        statement = statement.where(KnowledgeChunkRow.chunk_id > after_chunk_id)
    result = await session.execute(
        statement.order_by(KnowledgeChunkRow.chunk_id).limit(limit).execution_options(populate_existing=True)  # 刷新 Session 已缓存对象，避免读取旧属性。
    )
    return list(result.scalars())


async def count_chunks(
    session: AsyncSession, *, index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """统计一个 profile 的权威 Child 数量；数量检测不能代替完整内容摘要校验。"""

    result = await session.execute(
        select(func.count()).select_from(KnowledgeChunkRow).where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
        )
    )
    return int(result.scalar_one())


async def mark_chunks_ready(
    session: AsyncSession,
    chunk_ids: Sequence[str],
    *,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """将成功完成 ingestion 的一批 PENDING Child 改为 READY，返回实际更新行数。

    调用方应在外部写入成功后执行；本函数本身不检查 Milvus，也不提交事务。
    """

    unique_ids = set(chunk_ids)  # 同一批重复 ID 只更新一次。
    if not unique_ids:
        return 0
    result = await session.execute(
        update(KnowledgeChunkRow)
        .where(
            KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value,
            KnowledgeChunkRow.chunk_id.in_(unique_ids),
            KnowledgeChunkRow.ingestion_status
            == IngestionStatus.PENDING.value,  # 已 READY 的行不重写时间，重复标记是无操作。
        )
        .values(
            ingestion_status=IngestionStatus.READY.value,
            ready_at=func.now(),
            updated_at=func.now(),
        )
    )
    return int(result.rowcount or 0)  # 返回本次实际改变的记录数，不是请求 ID 数。


async def count_by_status(
    session: AsyncSession,
    status: IngestionStatus,
    *,
    index_profile: IndexProfile = IndexProfile.BASELINE,
) -> int:
    """统计指定 profile、指定 ingestion 状态的 Child 数量，不代表 serving 数据量。"""

    result = await session.execute(
        select(func.count())
        .select_from(KnowledgeChunkRow)
        .where(KnowledgeChunkRow.ingestion_status == status.value)
        .where(KnowledgeChunkRow.index_profile == IndexProfile(index_profile).value)
    )
    return int(result.scalar_one())
