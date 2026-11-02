"""将 PostgreSQL 权威 Child 物化到 Milvus，不重新切分原始文档。

ingest_pending：只处理 PENDING，Milvus 写入和 flush 成功后才将 PG 标为 READY。
rebuild_collection：读取 PENDING 和 READY 构建完整搜索副本，不修改原入库状态。
PG 与 Milvus 不共享事务；通过稳定 Chunk ID、upsert 和失败重试协调一致性。
本模块不写 Parent/Link，也不自动激活 serving route。
"""

from __future__ import annotations

from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.embeddings import EmbeddingClient, get_embedding_client
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.repository import (
    count_chunks,
    list_pending_chunks,
    list_rebuild_chunks,
    mark_chunks_ready,
    bind_collection,
)
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.serving import collection_write_guard, get_accepted_build, validate_serving_collection


DEFAULT_BATCH_SIZE = 32  # 默认一次向量化及写入的 Child 数量，不是文本字符数。
MAX_BATCH_SIZE = 64  # 限制单批请求规模；具体参数由 _validate_options() 检查。


async def ingest_pending(
    *,
    collection: str,
    index_profile: IndexProfile = IndexProfile.BASELINE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None,
    milvus_client: MilvusClient | None = None,
) -> int:
    """增量入库：按 profile 分批处理待写 Child，返回本次实际改为 READY 的行数。

    目标必须是物理 collection，调用方可注入会话工厂与客户端用于测试。
    未完成的批次不标 READY；重试复用同一 Chunk ID，不因 upsert 新增重复主键记录。
    Embedding 等外部调用可能重复发生，不保证模型调用或计费恰好一次。
    """

    _validate_options(collection, batch_size)

    profile = IndexProfile(index_profile)  # 限定为当前支持的 Baseline 或 Parent-Child profile。
    # 先只读检查 Milvus owner，再登记 PG 绑定；即使队列为空，也不能接受明显冲突的目标。
    await _preflight_collection(collection, profile, milvus_client)
    sessions = session_factory or get_session_factory()  # 注入测试会话或复用项目默认工厂。
    async with sessions() as session:
        async with session.begin():  # 此事务只负责目标绑定，离开该块时提交并释放绑定锁。
            await bind_collection(
                session, collection=collection, index_profile=profile, purpose="INGESTION",
            )
            
    completed = 0  # 累计实际发生 PENDING → READY 的行数，不是所有已存在 Child 的总数。
    while True:
        async with sessions() as session:
            pending = await list_pending_chunks(session, limit=batch_size, index_profile=profile)  # 按稳定 ID 排序读取。
        if not pending:  # 没有待处理数据时结束，不需要重新写入已有 READY 行。
            return completed

        # 与验收使用相同的物化锁；已被验收冻结的目标不能原地写入。
        # 正常 INGESTION target 不允许被验收，因此可以持续增量写入。
        async with collection_write_guard(sessions, collection):
            await _embed_and_upsert(
                pending,
                collection=collection,
                embedding_client=embedding_client,
                milvus_client=milvus_client,
                index_profile=profile,
            )

        # 必须等 Embedding、upsert、flush 全部成功，再单独提交 PG 状态更新。
        # 若 Milvus 成功后进程中断，PG 仍可能 PENDING；下次安全重写同一 ID 后再标 READY。
        async with sessions() as session:
            async with session.begin():
                completed += await mark_chunks_ready(
                    session, [row.chunk_id for row in pending], index_profile=profile,
                )


async def rebuild_collection(
    *,
    collection: str,
    index_profile: IndexProfile = IndexProfile.BASELINE,
    batch_size: int = DEFAULT_BATCH_SIZE,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None,
    milvus_client: MilvusClient | None = None,
) -> int:
    """用持久化 Child 重建并验收完整的 Milvus collection，不切换线上路由。

    同时读取 PENDING/READY，保留 PG 中原状态，不重跑 Baseline 或 Parent-Child 策略。
    使用新的空目标，或重试同一个未验收的中断目标；upsert 不会自动删除目标中多余旧行。
    正常新建路径返回本轮遍历写入行数；已验收目标的重试路径返回验收凭据中的来源数量。
    """

    _validate_options(collection, batch_size)
    profile = IndexProfile(index_profile)
    # 重建同样必须先检查外部归属，避免 PG 绑定先占用明显不兼容的目标。
    await _preflight_collection(collection, profile, milvus_client)
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        async with session.begin():
            await bind_collection(
                session, collection=collection, index_profile=profile, purpose="REBUILD",
            )
    async with sessions() as session:
        accepted = await get_accepted_build(session, collection)
    if accepted is not None:
        # 已验收目标视为不可变：只重新校验，不重新向量化或覆盖写入。
        # 来源已变化时校验会失败，应使用新的 REBUILD collection，而非修改旧快照。
        receipt = await validate_serving_collection(
            collection=collection, index_profile=profile, session_factory=sessions, milvus_client=milvus_client,
        )
        return receipt.source_chunk_count
    async with sessions() as session:
        expected_count = await count_chunks(session, index_profile=profile)  # 记录起始数量，辅助发现来源变化。
    after_chunk_id = None  # 首批无游标；后续读取 ID 严格大于上一批末尾的记录。
    written = 0  # 本轮已处理行数；不是保证“历史从未写入过”的新增向量数。
    while True:
        async with sessions() as session:
            chunks = await list_rebuild_chunks(
                session, limit=batch_size, after_chunk_id=after_chunk_id, index_profile=profile,
            )
        if not chunks:  # 游标遍历结束，进入完整性检查和验收。
            if written == 0:  # 空来源也需有真实 collection，才能执行后续验收。
                def ensure_empty_collection() -> None:
                    """来源为空时创建或检查目标 schema/owner，不写入任何 Child。"""

                    client = milvus_client or milvus.get_client()
                    milvus.ensure_collection(client, collection, index_profile=profile)

                await milvus.acall(ensure_empty_collection)  # 同步客户端操作放入 Milvus 专用执行线程。
            async with sessions() as session:
                actual_count = await count_chunks(session, index_profile=profile)
            if actual_count != expected_count:  # 数量变化说明重建期间来源新增或减少，不能直接宣告成功。
                raise RuntimeError(
                    "authoritative knowledge changed during rebuild; rerun rebuild"
                )
            # 数量相同不代表内容相同：还须逐字段对照 PG/Milvus，持久化成功验收并冻结目标。
            # 完整 rebuild 并非从开始就持来源独占锁，最终验收用于检查物化结果是否完整一致。
            await validate_serving_collection(
                collection=collection, index_profile=profile,
                session_factory=sessions, milvus_client=milvus_client,
            )
            return written  # 只有验收成功才正常返回；验收不自动触发 activation。

        async with collection_write_guard(sessions, collection):  # 每个实际写入批次与验收互斥。
            await _embed_and_upsert(
                chunks,
                collection=collection,
                embedding_client=embedding_client,
                milvus_client=milvus_client,
                index_profile=profile,
            )
        written += len(chunks)  # 批次成功后才推进统计与游标。
        after_chunk_id = chunks[-1].chunk_id  # ID 字符串排序游标，不是原文序号或数据库 OFFSET。


async def _preflight_collection(
    collection: str, profile: IndexProfile, client: MilvusClient | None,
) -> None:
    """只读预检目标是否与 profile 兼容；不创建、不 load collection，不写 PG。

    目标不存在时允许后续创建；真正写入前 ensure_collection() 还会再次检查归属。
    """

    def check() -> None:
        """将同步 Milvus 客户端访问封装成无参数任务，在专用线程执行。"""

        milvus.validate_collection_for_profile(client or milvus.get_client(), collection, profile)

    await milvus.acall(check)


def _validate_options(collection: str, batch_size: int) -> None:
    """检查非空目标名称、拒绝旧 active alias，并限定单批数量范围。"""

    if not collection or not collection.strip():
        raise ValueError("a physical collection name is required")
    if collection == milvus.ACTIVE_COLLECTION_ALIAS:
        raise ValueError("ingestion requires a physical collection, not the active alias")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


async def _embed_and_upsert(
    chunks: list[KnowledgeChunkRow],
    *,
    collection: str,
    embedding_client: EmbeddingClient | None,
    milvus_client: MilvusClient | None,
    index_profile: IndexProfile,
) -> None:
    """对一批 Child 正文生成向量，补齐搜索载荷，再 ensure/upsert/flush。

    任何一步异常均向上传播；本函数不更新 PG 状态，不提交权威数据事务。
    """

    embedder = embedding_client or get_embedding_client()  # 复用默认上游连接，或使用注入客户端。
    vectors = await embedder.embed_texts([row.content for row in chunks])  # 仅嵌入正文，不拼接标题或 Parent。
    rows = [
        {
            "chunk_id": row.chunk_id,
            "document_id": row.document_id,
            "document_type": row.document_type,
            "document_version": row.document_version,
            "title": row.title,
            "section_path": row.section_path,
            "content": row.content,
            "model_id": row.model_id,
            "source_uri": row.source_uri,
            "page_start": row.page_start,
            "page_end": row.page_end,
            "dense_vector": vector,  # 向量和来源业务字段一起写入，后续检索可与 PG 权威载荷核对。
        }
        for row, vector in zip(chunks, vectors, strict=True)  # 向量必须与 Child 一一对应，数量不同直接失败。
    ]

    def write_batch() -> None:
        """在同一同步执行任务中检查/创建目标、按稳定 ID upsert，再等待 flush。"""

        client = milvus_client or milvus.get_client()
        milvus.ensure_collection(client, collection, index_profile=index_profile)  # 写前再次校验 owner 和 schema。
        milvus.upsert_chunks(client, rows, collection)  # 同一 Chunk ID 重试覆盖，不生成另一个随机主键。
        milvus.flush(client, collection)  # 等此步骤成功后，上层才允许把 PG 标为 READY。

    await milvus.acall(write_batch)  # 将同步 Milvus 操作交给专用线程，避免直接阻塞异步事件循环。
