"""聊天入口的业务参数；操作者和权限不由请求体或模型提供。"""
from typing import Literal, Self
from pydantic import Field, model_validator
from risk_platform.memory.contracts import Identity, NonBlank, MemoryContract, MemoryContent


class ChatRequest(MemoryContract):
    request_id: Identity  # 同一用户重试同一请求必须保持此 ID 和正文不变。
    conversation_id: Identity | None = None
    message: NonBlank = Field(max_length=8000)
    action: Literal["CHAT", "READ_INVESTIGATION", "START_INVESTIGATION"] = "CHAT"
    investigation_id: Identity | None = None
    model_id: Identity | None = None

    @model_validator(mode="after")
    def action_parameters(self) -> Self:
        if self.action == "READ_INVESTIGATION" and self.investigation_id is None:
            raise ValueError("查询进度必须明确指定案件")
        if self.action == "START_INVESTIGATION" and (self.model_id is None or self.investigation_id is not None):
            raise ValueError("发起新调查必须指定模型，不能覆盖已有案件")
        if self.action == "CHAT" and self.investigation_id is not None:
            raise ValueError("普通聊天不隐式读取案件，请显式选择查询进度")
        return self


class ChatDraft(MemoryContract):
    answer: NonBlank = Field(max_length=16000)
    memory: MemoryContent = Field(default_factory=MemoryContent)


class ChatResponse(MemoryContract):
    conversation_id: Identity
    turn_id: Identity
    status: Literal["SUCCEEDED", "FAILED"]
    answer: NonBlank
    investigation_id: Identity | None = None
    error_code: Identity | None = None
