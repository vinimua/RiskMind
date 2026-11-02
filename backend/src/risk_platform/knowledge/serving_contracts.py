"""定义检索服务阶段共享的数据结构及其合法性规则。

RetrievalRouteSnapshot：本次请求固定使用哪个策略、物理集合和路由版本。
CollectionBuildAcceptance：一次成功构建验收的记录，供后续显式激活使用。
KnowledgeContext：最终供阅读或生成使用的上下文，以及触发它的 Child 命中。

本文件只定义和校验数据，不访问 PG/Milvus，不执行搜索、激活或 Parent 解析。
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.contracts import KnowledgeHit, _NonBlankString
from risk_platform.knowledge.indexing.profiles import IndexProfile


class RetrievalRouteSnapshot(BaseModel):
    """一次请求的路由快照；后续搜索与上下文解析继续携带同一份快照。

    即使管理员中途切换线上集合，本次请求也继续使用已经解析出的集合。
    “只解析一次”由调用流程保证，模型本身不会查询或锁住数据库路由。
    """

    # 禁止额外字段与字段重新赋值；这不是数据库不可变约束或递归深冻结。
    model_config = ConfigDict(extra="forbid", frozen=True)
    index_profile: IndexProfile  # Baseline / Parent-Child 的固定策略版本。
    collection: _NonBlankString  # 物理集合名；非空校验不负责检查集合是否真实存在。
    route_version: int = Field(gt=0, strict=True)  # 正整数；拒绝 "1"、1.0 和 True 等替代类型。


class CollectionBuildAcceptance(BaseModel):
    """成功验收结果的数据契约，不代表已经激活，也不等同于 Child 的 READY。

    仅表达已完成且通过的结果，不用它保存 BUILDING/FAILED 等中间状态。
    集合归属、REBUILD 用途与实际载荷一致性由 serving.py 检查，本模型不访问外部存储。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    build_id: _NonBlankString  # 本次验收记录身份，区别于文档策略构建记录的身份。
    index_profile: IndexProfile
    collection: _NonBlankString
    source_chunk_count: int = Field(ge=0, strict=True)  # 验收来源 Child 数量；空来源允许为 0。
    # Literal 限定字段允许的值；省略时使用默认成功值，不是自动执行了构建或验收。
    build_complete: Literal[True] = True
    validation_status: Literal["PASSED"] = "PASSED"
    # 两份摘要采用 64 位小写十六进制格式；正则只查格式，不会重新计算 SHA-256。
    authoritative_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime  # 必须带时区信息，便于不同环境按同一时间线比较。
    validated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_acceptance(self) -> Self:
        """单字段校验完成后，再检查摘要相等和时间先后两个跨字段不变量。"""

        # 格式合法还不够：验收摘要必须对应这次记录的权威来源摘要。
        if self.validation_digest != self.authoritative_snapshot_digest:
            raise ValueError("validation must match the authoritative source snapshot")
        if self.validated_at < self.completed_at:  # 相同时间允许，但验收不能早于构建完成。
            raise ValueError("validation cannot precede build completion")
        return self  # after 校验器返回已校验的模型；Self 表示当前模型类型。


class KnowledgeContext(BaseModel):
    """最终阅读上下文：Baseline 返回 Child 正文，Parent-Child 返回 Parent 原文。

    supporting_hits 保留实际命中的 Child 及其来源和排名。
    Context 是知识线索，不自动成为经过业务证据门禁确认的 Evidence。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    context_id: _NonBlankString  # Baseline 对应 Child ID；Parent-Child 对应 Parent ID。
    content: _NonBlankString  # 实际提供给后续阅读/生成的正文，不要求与单个 Child 正文相同。
    # 至少一条 Child 命中；元组允许多条，去重、归属及排名由 context_resolution.py 校验。
    supporting_hits: tuple[KnowledgeHit, ...] = Field(min_length=1)
