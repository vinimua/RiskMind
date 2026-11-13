"""聊天入口的业务参数；操作者和权限不由请求体或模型提供。"""
from typing import Literal, Self
from pydantic import Field, model_validator
from risk_platform.memory.contracts import Identity, NonBlank, MemoryContract, MemoryContent


class ChatRequest(MemoryContract):
    request_id: Identity  # 同一用户重试同一请求必须保持此 ID 和正文不变。
    conversation_id: Identity | None = None
    message: NonBlank = Field(max_length=8000)
    investigation_id: Identity | None = None
    model_id: Identity | None = None

    @model_validator(mode="after")
    def explicit_target(self) -> Self:
        if self.investigation_id is not None and self.model_id is not None:
            raise ValueError("不能同时绑定已有案件和新调查模型")
        return self


class ChatDraft(MemoryContract):
    answer: NonBlank = Field(max_length=16000)
    memory: MemoryContent = Field(default_factory=MemoryContent)


class ProgressReadArguments(MemoryContract):
    """模型只能指定查询对象，不能填写 actor、权限或运行状态。"""
    investigation_id: Identity


class StartInvestigationArguments(MemoryContract):
    """只提出目标模型；问题原文、案件身份和确认事实均由服务器提供。"""
    model_id: Identity


class ToolInvocation(MemoryContract):
    """原生工具请求的内部表示；不是已授权、已确认或已执行的事实。"""
    call_id: Identity
    name: Literal["get_investigation_progress", "start_investigation"]
    arguments: ProgressReadArguments | StartInvestigationArguments

    @model_validator(mode="after")
    def matching_arguments(self) -> Self:
        reading = self.name == "get_investigation_progress"
        if reading != isinstance(self.arguments, ProgressReadArguments):
            raise ValueError("工具名称与参数契约不匹配")
        return self


class ChatResponse(MemoryContract):
    conversation_id: Identity
    turn_id: Identity
    status: Literal["SUCCEEDED", "FAILED"]
    answer: NonBlank
    investigation_id: Identity | None = None
    error_code: Literal["FORBIDDEN", "NOT_FOUND", "CONFLICT", "DEPENDENCY"] | None = None

    @model_validator(mode="after")
    def valid_outcome(self) -> Self:
        if (self.status == "FAILED") != (self.error_code is not None):
            raise ValueError("失败必须提供错误码，成功不能包含错误码")
        return self


class EntryConfirmation(MemoryContract):
    """由已认证用户确认已保存的调用；不能传入新工具名、新目标或 actor。"""
    invocation_id: Identity
    decision_digest: NonBlank = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = Field(strict=True)


class EntryActionState(MemoryContract):
    """按原 turn 查询的审计视图；STARTED/UNCERTAIN 不是允许自动重试的信号。"""
    turn_id: Identity
    conversation_id: Identity
    status: Literal["DECIDING", "ANSWERED", "REJECTED", "PENDING_CONFIRMATION", "READY",
                    "STARTED", "SUCCEEDED", "FAILED", "UNCERTAIN", "CANCELLED"]
    decision: ChatDraft | ToolInvocation | None = None
    decision_digest: str | None = None
    invocation_id: Identity | None = None
    investigation_id: Identity | None = None
    result: ChatResponse | None = None
