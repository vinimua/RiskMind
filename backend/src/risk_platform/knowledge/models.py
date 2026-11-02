"""Knowledge 模块的 SQLAlchemy 表定义，PostgreSQL 是权威存储。

构建数据：Document → Build → Parent / Child / Link。
上线数据：Collection → CollectionBuild（验收）→ Activation（历史）→ Route（当前指针）。
这些类描述数据库结构，不执行分块或向量检索；Milvus 是可重建的搜索副本。
“不可变”“只追加”和验收/激活流程还依赖 repository.py、serving.py 的业务约束，
不能仅凭类名或注释推断数据库自动禁止 UPDATE / DELETE。


| 分组 | 表模型 | 保存什么 |
|---|---|---|
| 原始文档与构建 | `KnowledgeDocumentRow` | 完整原始文档及版本 |
| | `KnowledgeBuildRow` | 某文档版本在某策略下的构建摘要 |
| 构建产物 | `KnowledgeChunkRow` | 搜索 Child |
| | `KnowledgeContextRow` | 阅读 Parent |
| | `KnowledgeContextLinkRow` | Child→Parent 关系 |
| 物化与上线 | `KnowledgeCollectionRow` | 物理 collection 的归属和用途 |
| | `KnowledgeCollectionBuildRow` | collection 成功验收凭据 |
| | `KnowledgeServingActivationRow` | 每次成功上线的历史 |
| | `KnowledgeServingRouteRow` | 当前生效的线上指针 |
"""

from __future__ import annotations  # 延迟求值类型标注。

from datetime import datetime  # ORM 时间字段对应的 Python 类型。
from enum import StrEnum  # 用字符串枚举表达入库状态，持久化时使用 .value。

from sqlalchemy import (
    # 列类型、数据库约束、索引，以及 SQL 表达式构建工具。
    Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import Mapped, mapped_column  # Mapped 标注 ORM 属性类型，mapped_column 定义实际列。

from risk_platform.db.database import Base  # 共同 ORM 基类，所有表会登记到 Base.metadata。


class IngestionStatus(StrEnum):
    """Child 写入 ingestion target 的状态，不代表某个 serving collection 已验收上线。"""

    PENDING = "PENDING"  # 待向量化或待完成写入；重试仍处理该状态。
    READY = "READY"  # 已完成该 profile 的 ingestion 写入；不是 serving eligibility。


class KnowledgeChunkRow(Base):
    """搜索 Child 的权威正文；Baseline 片段和 Parent-Child 的 Child 都存此表。

    联合主键是 (chunk_id, index_profile)，相同片段键可在不同 profile 下隔离保存。
    字段大多对应 KnowledgeChunk；入库状态和时间属于持久化层附加信息。
    """

    __tablename__ = "knowledge_chunks"  # 数据库中的实际表名。
    __table_args__ = (
        # CHECK 在数据库层限制 profile 值，绕过 Python 插入也要满足。
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_chunks_profile",
        ),
        CheckConstraint(
            "ingestion_status IN ('PENDING', 'READY')",  # 只允许两种已定义的入库状态。
            name="ck_knowledge_chunks_ingestion_status",
        ),
        CheckConstraint(
            # 起止页要么同时为空，要么均有值，且起始页为正、结束页不小于起始页。
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start IS NOT NULL AND page_end IS NOT NULL "
            "AND page_start > 0 AND page_end >= page_start)",
            name="ck_knowledge_chunks_page_range",
        ),
        Index(
            "ix_knowledge_chunks_status_chunk_id",  # 支持按 profile + 状态筛选并按 chunk_id 分页。
            "index_profile",
            "ingestion_status",
            "chunk_id",
        ),
        Index(
            "ix_knowledge_chunks_document_version",  # 支持查询某个 profile 下指定文档版本的片段。
            "index_profile",
            "document_id",
            "document_version",
        ),
    )

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # 确定性片段 ID，联合主键的一部分。
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 来源文档 ID；本列自身未声明文档表外键。
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)  # 来源文档内容版本。
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)  # 文档类型的字符串值。
    title: Mapped[str] = mapped_column(String(512), nullable=False)  # 文档标题，用于展示和权威数据校验。
    section_path: Mapped[str] = mapped_column(String(1024), nullable=False)  # 章节路径，允许空串但不允许 SQL NULL。
    content: Mapped[str] = mapped_column(Text, nullable=False)  # Child 正文；不在此表保存 embedding。
    model_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 关联的风控模型，通用知识可为空。
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)  # 来源地址，由业务契约校验其格式。
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 内容涉及的最小页码。
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 内容涉及的最大页码。
    ingestion_status: Mapped[str] = mapped_column(
        String(16),  # 数据库保存字符串，不直接保存 Python 枚举对象。
        nullable=False,  # 每条 Child 都必须有状态。
        default=IngestionStatus.PENDING.value,  # SQLAlchemy 插入且未提供值时使用的默认值。
        server_default=IngestionStatus.PENDING.value,  # 数据库插入省略此列时的默认值。
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),  # PostgreSQL 使用带时区语义的时间类型。
        nullable=False,
        server_default=func.now(),  # 由数据库在插入时生成创建时间。
    )
    updated_at: Mapped[datetime] = mapped_column(  # 最近一次由当前 ORM 更新路径维护的时间。
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),  # SQLAlchemy 更新时补入 now()；不是自动覆盖所有外部 SQL 的数据库触发器。
    )
    ready_at: Mapped[datetime | None] = mapped_column(  # 完成 ingestion 写入的时间，待处理时可为空。
        DateTime(timezone=True),
        nullable=True,
    )
    index_profile: Mapped[str] = mapped_column(
        # 第二个主键列：与 chunk_id 共同唯一；默认 Baseline 兼容早期写入。
        String(64), primary_key=True, default="baseline-v1", server_default="baseline-v1",
    )


class KnowledgeDocumentRow(Base):
    """跨 profile 共享的 canonical 文档，同一文档 ID + 版本的内容不允许业务层覆盖。

    source_json 保存完整 KnowledgeDocument，而非只有标题；旧来源可在使用时补登记。
    联合主键防止重复文档身份，但禁止改写内容需要 repository 的冲突检查。
    """

    __tablename__ = "knowledge_documents"
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # 联合主键：文档 ID。
    document_version: Mapped[str] = mapped_column(String(64), primary_key=True)  # 联合主键：内容版本。
    source_json: Mapped[str] = mapped_column(Text, nullable=False)  # 规范序列化的文档 JSON，列类型是 TEXT。


class KnowledgeBuildRow(Base):
    """某文档版本在指定 profile 下的构建指纹，用来判断重复 staging 是否一致。

    这里记录的是纯构建结果，不是 Milvus collection 的完成或验收状态。
    prepared_digest 包括准备结果中的内容及顺序，业务层按不可变记录使用。
    """

    __tablename__ = "knowledge_builds"
    __table_args__ = (
        # 两列共同引用同一份 canonical 文档，不是两个相互独立的单列外键。
        ForeignKeyConstraint(
            ["document_id", "document_version"],
            ["knowledge_documents.document_id", "knowledge_documents.document_version"],
        ),
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_builds_profile",
        ),
    )

    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)  # 三列联合主键中的策略维度。
    document_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # 构建的来源文档。
    document_version: Mapped[str] = mapped_column(String(64), primary_key=True)  # 构建的来源版本。
    prepared_digest: Mapped[str] = mapped_column(String(64), nullable=False)  # 规范化 PreparedKnowledge 的 SHA256 摘要。


class KnowledgeContextRow(Base):
    """Parent 权威正文；Baseline 不需要为每个 Child 额外创建此表记录。"""

    __tablename__ = "knowledge_contexts"
    __table_args__ = (
        # Parent 必须属于对应 profile、文档 ID、版本的那次纯构建。
        ForeignKeyConstraint(
            ["index_profile", "document_id", "document_version"],
            ["knowledge_builds.index_profile", "knowledge_builds.document_id",
             "knowledge_builds.document_version"],
        ),
        CheckConstraint(
            # 与 Child 相同，数据库保证页码成对出现且范围合法。
            "(page_start IS NULL AND page_end IS NULL) OR "
            "(page_start > 0 AND page_end >= page_start "
            "AND page_start IS NOT NULL AND page_end IS NOT NULL)",
            name="ck_knowledge_contexts_pages",
        ),
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)  # 联合主键中的策略隔离维度。
    context_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # Parent 确定性 ID。
    document_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 原文档 ID，参与构建外键。
    document_version: Mapped[str] = mapped_column(String(64), nullable=False)  # 原文档版本，参与构建外键。
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)  # 继承原文档类型。
    title: Mapped[str] = mapped_column(String(512), nullable=False)  # 继承原文档标题。
    section_path: Mapped[str] = mapped_column(String(1024), nullable=False)  # Parent 所属章节。
    content: Mapped[str] = mapped_column(Text, nullable=False)  # Parent 原文；不通过带 overlap 的 Child 拼接。
    model_id: Mapped[str | None] = mapped_column(String(128))  # Optional 标注使未显式设置的 nullable 推断为 True。
    source_uri: Mapped[str] = mapped_column(String(2048), nullable=False)  # Parent 来源 URI。
    page_start: Mapped[int | None] = mapped_column(Integer)  # 可空起始页，受页码 CHECK 约束。
    page_end: Mapped[int | None] = mapped_column(Integer)  # 可空结束页，受页码 CHECK 约束。


class KnowledgeContextLinkRow(Base):
    """当前 profile 下 Child 到 Parent 的关联，以及 Child 在父内的顺序。

    联合主键保证每个 Child 至多一条 Link；每个 Parent-Child Child 必须有 Link，
    以及父子来源一致等更强规则，由构建契约、staging 和解析逻辑共同校验。
    """

    __tablename__ = "knowledge_context_links"
    __table_args__ = (
        # 引用实际存在的同 profile Child，避免映射到其他策略的同名 ID。
        ForeignKeyConstraint(
            ["chunk_id", "index_profile"],
            ["knowledge_chunks.chunk_id", "knowledge_chunks.index_profile"],
        ),
        ForeignKeyConstraint(
            ["index_profile", "context_id"],  # Parent 也必须存在于同一个 profile。
            ["knowledge_contexts.index_profile", "knowledge_contexts.context_id"],
        ),
        UniqueConstraint("index_profile", "context_id", "child_ordinal"),  # 同一 Parent 内不允许两个 Child 占同一序号。
        CheckConstraint("child_ordinal >= 0", name="ck_knowledge_context_links_ordinal"),  # 从 0 开始；连续性不由此 CHECK 保证。
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)  # Child 和 Parent 的共同策略。
    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # 与 profile 合起来唯一，限制一个 Child 至多一个 Parent。
    context_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 被关联的 Parent ID。
    child_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)  # Parent 内的 Child 顺序，不是检索 rank。


class KnowledgeCollectionRow(Base):
    """登记 Milvus 物理 collection 的 profile 归属和写入用途，不保存向量本身。

    INGESTION：一个 profile 唯一的持续增量写入目标。
    REBUILD：可创建多个，用于完整快照重建；只有验收并显式激活后才可 serving。
    业务层禁止已有绑定更换归属；purpose 不直接表示是否正在上线。
    """

    __tablename__ = "knowledge_collections"
    __table_args__ = (
        UniqueConstraint("collection", "index_profile", name="uq_knowledge_collections_profile"),  # 为后续两列复合外键提供唯一目标。
        CheckConstraint("purpose IN ('INGESTION', 'REBUILD')", name="ck_knowledge_collections_purpose"),  # 不用 purpose 表示 ACTIVE。
        CheckConstraint(
            "index_profile IN ('baseline-v1', 'parent-child-v1')",
            name="ck_knowledge_collections_profile",
        ),
        Index(
            # 部分唯一索引：仅 INGESTION 行要求 profile 唯一，不限制同 profile 的 REBUILD 数量。
            "uq_knowledge_collections_ingestion_profile", "index_profile", unique=True,
            postgresql_where=text("purpose = 'INGESTION'"),  # PostgreSQL 的索引适用条件。
            sqlite_where=text("purpose = 'INGESTION'"),  # SQLite 测试使用相同条件，避免退化成全表唯一。
        ),
    )
    collection: Mapped[str] = mapped_column(String(255), primary_key=True)  # 物理 collection 名，不能同时登记两个 owner。
    index_profile: Mapped[str] = mapped_column(String(64), nullable=False)  # 该 collection 服务的索引策略。
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # INGESTION 或 REBUILD。


class KnowledgeCollectionBuildRow(Base):
    """物理 collection 成功完成物化和校验后的验收记录，业务层按不可变凭据使用。

    区别于 KnowledgeBuildRow：这里验证的是整个 profile 的 Child 集合与 Milvus 副本，
    不只是单份文档的纯构建结果。失败不写成功凭据；用途必须为 REBUILD 由 serving.py 检查。
    验收不等于激活，线上指针仍需单独切换。
    """

    __tablename__ = "knowledge_collection_builds"
    __table_args__ = (
        # 验收只能引用已登记且 profile 一致的物理 collection。
        ForeignKeyConstraint(["collection", "index_profile"],
                             ["knowledge_collections.collection", "knowledge_collections.index_profile"]),
        UniqueConstraint("build_id", "index_profile", "collection", name="uq_knowledge_collection_build_identity"),  # 激活记录按三列共同引用此身份。
        UniqueConstraint("collection", name="uq_knowledge_collection_build_collection"),  # 同一物理 collection 只有一份成功验收。
        CheckConstraint("source_chunk_count >= 0", name="ck_collection_build_count"),  # 空集合可为 0，不能为负。
        CheckConstraint("build_complete AND validation_status = 'PASSED'", name="ck_collection_build_passed"),  # 本表只接受成功且完整的验收。
    )
    build_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # 此次成功 collection 验收的稳定标识。
    collection: Mapped[str] = mapped_column(String(255), nullable=False)  # 已完整物化的目标物理 collection。
    index_profile: Mapped[str] = mapped_column(String(64), nullable=False)  # 被验收的策略。
    source_chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)  # 验收时权威 Child 集合数量。
    authoritative_snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)  # PG Child 业务载荷摘要；当前不覆盖 Parent/Link。
    validation_digest: Mapped[str] = mapped_column(String(64), nullable=False)  # 对照 Milvus 校验得到的摘要，业务层要求与权威摘要一致。
    build_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)  # 必须为 True，不用于记录进行中的 build。
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)  # 当前本表只允许 PASSED。
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 记录完成物化的时间。
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 记录校验通过的时间。


class KnowledgeServingActivationRow(Base):
    """每次成功激活的审计历史和幂等回执，业务层只追加，不覆盖旧事件。

    与当前路由同事务提交；相同幂等请求重试应返回原回执，而不是再增加版本。
    权限、预期版本、来源和 Milvus 复核均由 serving.py 执行，本表字段不代替鉴权。
    """

    __tablename__ = "knowledge_serving_activations"
    __table_args__ = (
        # 三列外键确保引用的是同一验收的 build/profile/collection 组合，而非各自独立合法。
        ForeignKeyConstraint(["build_id", "index_profile", "collection"],
                             ["knowledge_collection_builds.build_id", "knowledge_collection_builds.index_profile",
                              "knowledge_collection_builds.collection"]),
        UniqueConstraint("index_profile", "route_version", name="uq_serving_activation_version"),  # 每个 profile 的每个路由版本只有一次激活历史。
        CheckConstraint("route_version > 0", name="ck_serving_activation_version"),  # 首次激活从 1 开始，不保存版本 0 事件。
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)  # 全表唯一请求键，防止重试生成重复激活。
    index_profile: Mapped[str] = mapped_column(String(64), nullable=False)  # 本次切换的策略。
    collection: Mapped[str] = mapped_column(String(255), nullable=False)  # 本次上线的物理 collection。
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 上线所依据的成功验收凭据。
    route_version: Mapped[int] = mapped_column(Integer, nullable=False)  # 此次激活分配的 profile 路由版本。
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 激活事件发生时间。
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)  # 已通过授权检查的操作者标识。


class KnowledgeServingRouteRow(Base):
    """每个 profile 当前生效的 serving 指针，不是完整切换历史。

    profile 是单列主键，因此每个 profile 至多一条当前路由；没有记录就明确失败，
    不回退旧 alias。此记录可在成功激活事务中更新，旧历史留在 Activation 表。
    """

    __tablename__ = "knowledge_serving_routes"
    __table_args__ = (
        # 当前版本必须存在对应激活历史；其余审计字段与历史一致由激活事务保证。
        ForeignKeyConstraint(["index_profile", "route_version"],
                             ["knowledge_serving_activations.index_profile", "knowledge_serving_activations.route_version"]),
        ForeignKeyConstraint(["build_id", "index_profile", "collection"],  # 当前目标也必须指向同 profile 的真实验收组合。
                             ["knowledge_collection_builds.build_id", "knowledge_collection_builds.index_profile",
                              "knowledge_collection_builds.collection"]),
    )
    index_profile: Mapped[str] = mapped_column(String(64), primary_key=True)  # 每个策略当前最多一个 serving 目标。
    collection: Mapped[str] = mapped_column(String(255), nullable=False)  # 在线请求实际应搜索的物理 collection。
    build_id: Mapped[str] = mapped_column(String(128), nullable=False)  # 当前目标的验收记录。
    route_version: Mapped[int] = mapped_column(Integer, nullable=False)  # 检索开始时复制到 RetrievalRouteSnapshot。
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # 当前路由最近一次成功激活时间。
    activated_by: Mapped[str] = mapped_column(String(128), nullable=False)  # 当前路由的激活操作者。
