"""v2 稳定业务输入；v1 原文/摘要不改写，兼容通过显式投影比较。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_platform.execution.contracts import KnowledgeExecutionContext, KnowledgeSearchToolRequest
from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.indexing.profiles import IndexProfile
from .contracts import Identity, OperationInput, OperationType


class BusinessOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    investigation_id: Identity
    round_id: Identity
    plan_id: Identity
    task_id: Identity | None = None
    operation_type: OperationType
    tool_name: Literal["knowledge_search"] = "knowledge_search"
    model_name: Identity | None = None
    prompt_version: Identity | None = None
    max_top_k: int | None = Field(default=None, gt=0, strict=True)
    candidate_request: KnowledgeSearchToolRequest | None = None
    scope: KnowledgeFilters | None = None
    index_profile: IndexProfile | None = None
    execution_refs: tuple[Identity, ...] = ()

    @model_validator(mode="after")
    def stage_inputs(self) -> Self:
        if self.operation_type == OperationType.OBSERVATION:
            if self.task_id is not None or not self.execution_refs or len(set(self.execution_refs)) != len(self.execution_refs):
                raise ValueError("解释必须关联唯一执行引用，不绑定单个任务")
        elif self.task_id is None or self.execution_refs:
            raise ValueError("任务操作必须提供正式 task_id")
        if self.operation_type == OperationType.KNOWLEDGE_EXECUTION:
            if (self.candidate_request is None or self.scope is None or self.index_profile is None
                    or self.model_name is not None or self.prompt_version is not None or self.max_top_k is not None):
                raise ValueError("知识执行仅记录候选请求、生效业务范围与 profile")
        else:
            if (self.candidate_request is not None or self.scope is not None or self.index_profile is not None
                    or self.model_name is None or self.prompt_version is None):
                raise ValueError("模型阶段必须记录模型/提示词版本，不保存无关执行范围")
            if (self.operation_type == OperationType.QUERY_FORMULATION) != (self.max_top_k is not None):
                raise ValueError("只有查询形成阶段必须记录进入模型的 max_top_k")
        return self


def business_projection(value: OperationInput | BusinessOperationInput) -> BusinessOperationInput:
    if isinstance(value, BusinessOperationInput):
        return BusinessOperationInput.model_validate_json(value.model_dump_json())
    value = OperationInput.model_validate_json(value.model_dump_json())
    data = value.model_dump(exclude={"schema_version", "context"})
    if value.operation_type == OperationType.KNOWLEDGE_EXECUTION:
        data.update(scope=value.context.scope, index_profile=value.context.index_profile)
    elif value.operation_type == OperationType.QUERY_FORMULATION:
        data["max_top_k"] = value.context.max_top_k
    return BusinessOperationInput(**data)


class AttemptAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    investigation_id: Identity
    round_id: Identity
    actor_id: Identity
    action: Literal["investigation:continue"] = "investigation:continue"
    execution_context: KnowledgeExecutionContext

    @model_validator(mode="after")
    def actor_matches(self) -> Self:
        if self.actor_id != self.execution_context.actor_id:
            raise ValueError("尝试发起人与本次可信执行身份不一致")
        return self
