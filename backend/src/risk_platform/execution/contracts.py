"""Stage 7A：知识请求与执行事实契约，不预建其他 capability 的载荷。"""

from enum import StrEnum
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator, field_validator

from risk_platform.knowledge.context_resolution import ContextResolutionResult
from risk_platform.knowledge.contracts import KnowledgeFilters, KnowledgeSearchRequest, _NonBlankString
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
from risk_platform.workflow.contracts import CapabilityType


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExecutionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class ErrorCategory(StrEnum):
    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    DEPENDENCY = "DEPENDENCY"
    INTEGRITY = "INTEGRITY"
    INTERNAL = "INTERNAL"


class ExecutionError(ExecutionContract):
    category: ErrorCategory
    code: _NonBlankString
    message: _NonBlankString
    retryable: bool = Field(strict=True)


class KnowledgeSearchToolRequest(KnowledgeSearchRequest):
    """Query Formulation 的候选请求；不允许模型提供身份、profile 或 collection。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    top_k: int = Field(gt=0, strict=True)


class KnowledgeExecutionContext(ExecutionContract):
    """可信应用提供的权限与范围；不从模型输出反序列化为可信身份。"""

    actor_id: _NonBlankString
    index_profile: IndexProfile
    scope: KnowledgeFilters = Field(default_factory=KnowledgeFilters)
    max_top_k: int = Field(default=20, gt=0, strict=True)
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)


class KnowledgeExecutionPayload(ExecutionContract):
    """保留实际生效请求、原始 Child 与 Context，以便沿执行记录追溯来源。"""

    request: KnowledgeSearchToolRequest
    retrieved: DenseChildRetrievalResult
    resolved: ContextResolutionResult

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        if self.retrieved.snapshot != self.resolved.snapshot:
            raise ValueError("检索与上下文的路由快照不一致")
        hits = [item.hit for item in self.retrieved.hits]
        ids = [hit.source.chunk_id for hit in hits]
        if len(ids) != len(set(ids)) or [h.rank for h in hits] != list(range(1, len(hits) + 1)):
            raise ValueError("检索子块 ID 必须唯一，排名必须连续")
        if len(hits) > self.request.top_k:
            raise ValueError("检索结果超过请求上限")
        support = [hit for context in self.resolved.contexts for hit in context.supporting_hits]
        by_id = {hit.source.chunk_id: hit for hit in hits}
        if len(support) != len(hits) or {h.source.chunk_id for h in support} != set(ids):
            raise ValueError("上下文必须恰好覆盖全部命中子块")
        if any(hit != by_id[hit.source.chunk_id] for hit in support):
            raise ValueError("上下文支持子块与原始命中不一致")
        return self


class TaskExecutionResult(ExecutionContract):
    """一次调用尝试的事实；execution_id 不等于持久化幂等键。"""

    execution_id: _NonBlankString
    task_id: _NonBlankString
    capability: CapabilityType
    status: ExecutionStatus
    started_at: AwareDatetime
    completed_at: AwareDatetime
    # 无 tool_name 的既有知识记录保持原格式；新通用结果显式声明工具及结果版本。
    tool_name: str | None = Field(default=None, exclude_if=lambda value: value is None)
    result_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    payload: KnowledgeExecutionPayload | dict | None = None
    error: ExecutionError | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def legacy_payload(cls, value, info):
        if value is not None and info.data.get("tool_name") in (None, "knowledge_search"):
            return KnowledgeExecutionPayload.model_validate(value)
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValueError("执行完成时间不能早于开始时间")
        if self.status == ExecutionStatus.SUCCEEDED:
            if self.payload is None or self.error is not None:
                raise ValueError("成功知识执行必须包含载荷且不能包含错误")
            if self.tool_name is None:
                if not isinstance(self.payload, KnowledgeExecutionPayload) or self.capability != CapabilityType.KNOWLEDGE_RETRIEVAL:
                    raise ValueError("旧格式仅支持知识执行；其他结果必须声明工具及协议版本")
            else:
                # 延迟导入避免注册定义与契约的循环；动态模型只来自可信注册表。
                from risk_platform.execution.tools import tool_definition
                definition = tool_definition(self.tool_name)
                if definition.capability != self.capability or definition.result_version != self.result_version:
                    raise ValueError("工具能力或结果版本不匹配")
                definition.result_model.model_validate(self.payload.model_dump() if isinstance(self.payload, BaseModel) else self.payload)
        elif self.payload is not None or self.error is None:
            raise ValueError("失败或拒绝执行必须包含错误且不能包含部分载荷")
        return self
