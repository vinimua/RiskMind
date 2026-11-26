"""聊天入口的业务参数；操作者和权限不由请求体或模型提供。"""
from typing import Literal, Self
from pydantic import Field, model_validator
from risk_platform.memory.contracts import Identity, NonBlank, MemoryContract


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
    memory_candidate: NonBlank | None = Field(default=None, max_length=2000)


class ConversationSummaryDraft(MemoryContract):
    """只承载摘要正文；覆盖边界和版本由程序确定。"""
    summary: NonBlank = Field(max_length=12000)


class ReadChatHistoryRequest(MemoryContract):
    """只读取服务端绑定的当前会话，不接受用户或会话身份。"""
    from_turn: int = Field(ge=1, strict=True)
    to_turn: int = Field(ge=1, strict=True)
    read_interaction: int | None = Field(default=None, ge=0, strict=True,
        description="回看当时读取的笔记时指定该轮读取序号；不填则读取原始问答")
    start_paragraph: int = Field(default=0, ge=0, strict=True)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.to_turn < self.from_turn:
            raise ValueError("历史结束位置不能早于开始位置")
        if self.read_interaction is not None and self.from_turn != self.to_turn:
            raise ValueError("回查读取快照必须指定单个历史轮次")
        if self.read_interaction is None and self.start_paragraph:
            raise ValueError("完整问答不能按段落截取")
        return self


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
    error_code: Literal["FORBIDDEN", "NOT_FOUND", "CONFLICT", "DEPENDENCY", "CONTEXT_BUDGET", "COMPACTION_FAILED"] | None = None

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
