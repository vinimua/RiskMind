"""8A 账本契约：逻辑工作、调用尝试与已提交产物分别建模。"""

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, model_validator

from risk_platform.execution.contracts import KnowledgeExecutionContext, KnowledgeSearchToolRequest, TaskExecutionResult
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.observation import RoundObservation


Identity = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class LedgerContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1


def canonical_json(value: BaseModel) -> str:
    """对象键排序，列表及正文原样保留；NaN/Infinity 不允许进入摘要。"""
    return json.dumps(value.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def input_digest(value: BaseModel) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class InvestigationRecord(LedgerContract):
    investigation_id: Identity
    created_by: Identity  # 审计身份，不是读取或恢复权限凭据。
    planning_input: InitialPlanningInput


class RoundRecord(LedgerContract):
    investigation_id: Identity
    round_id: Identity
    round_number: int = Field(gt=0, strict=True)


class AcceptedPlan(LedgerContract):
    investigation_id: Identity
    round_id: Identity
    plan_id: Identity
    plan_version: int = Field(gt=0, strict=True)
    plan: InvestigationPlan

    @model_validator(mode="after")
    def pending_unique_tasks(self) -> Self:
        ids = [task.task_id for task in self.plan.tasks]
        for key in ids:
            TypeAdapter(Identity).validate_python(key)
        if not ids or len(ids) != len(set(ids)) or any(t.status != TaskStatus.PENDING for t in self.plan.tasks):
            raise ValueError("接受的计划必须包含身份唯一且全部 PENDING 的正式任务")
        return self


class OperationType(StrEnum):
    QUERY_FORMULATION = "QUERY_FORMULATION"
    KNOWLEDGE_EXECUTION = "KNOWLEDGE_EXECUTION"
    OBSERVATION = "OBSERVATION"


class OperationInput(LedgerContract):
    investigation_id: Identity
    round_id: Identity
    plan_id: Identity
    task_id: Identity | None = None
    operation_type: OperationType
    # 只序列化既有可信标量，不保存客户端、API Key 或 authorize。
    context: KnowledgeExecutionContext
    tool_name: Literal["knowledge_search"] = "knowledge_search"
    model_name: Identity | None = None
    prompt_version: Identity | None = None
    candidate_request: KnowledgeSearchToolRequest | None = None
    execution_refs: tuple[Identity, ...] = ()

    @model_validator(mode="after")
    def stage_inputs(self) -> Self:
        if self.operation_type == OperationType.OBSERVATION:
            if self.task_id is not None or not self.execution_refs or len(set(self.execution_refs)) != len(self.execution_refs):
                raise ValueError("解释操作必须关联本轮执行记录，不能绑定单个任务")
        elif self.task_id is None or self.execution_refs:
            raise ValueError("任务操作必须提供 task_id，不能携带整轮执行引用")
        if self.operation_type == OperationType.KNOWLEDGE_EXECUTION:
            if self.candidate_request is None or self.model_name is not None or self.prompt_version is not None:
                raise ValueError("知识执行输入必须包含候选请求，不包含解释模型参数")
        elif self.candidate_request is not None or self.model_name is None or self.prompt_version is None:
            raise ValueError("模型操作必须记录模型与提示词版本，不预填候选请求")
        return self


def operation_key(value: OperationInput) -> str:
    """身份摘要不包含业务输入；相同逻辑身份的输入变化必须触发冲突。"""
    identity = [value.investigation_id, value.round_id, value.plan_id, value.task_id, value.operation_type.value]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class AttemptStatus(StrEnum):
    STARTED = "STARTED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class CommittedArtifact(LedgerContract):
    """一种产物恰好占一个字段；effective request 保留在 execution.payload.request。"""
    query_request: KnowledgeSearchToolRequest | None = None
    execution: TaskExecutionResult | None = None
    observation: RoundObservation | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if sum(value is not None for value in (self.query_request, self.execution, self.observation)) != 1:
            raise ValueError("已提交产物必须且只能包含一种结果")
        return self
