"""个人记忆文档及维护建议；所有人由服务端提供，不进入模型参数。"""
from typing import Literal, Self
from pydantic import Field, model_validator
from .contracts import MemoryContract, Identity, NonBlank


class NoteContent(MemoryContract):
    title: NonBlank = Field(max_length=120)
    description: NonBlank = Field(max_length=300)
    content: NonBlank = Field(max_length=12000)


class PersonalNote(NoteContent):
    note_id: Identity
    version: int = Field(strict=True, ge=1)
    active: bool = True


class NoteCreate(NoteContent):
    request_id: Identity


class NoteUpdate(NoteCreate):
    expected_version: int = Field(strict=True, ge=1)
    active: bool = True


class MemorySettings(MemoryContract):
    read_enabled: bool = Field(default=True, strict=True, description="是否向聊天回答提供个人记忆")
    auto_write_enabled: bool = Field(default=True, strict=True, description="是否自动维护笔记；开启时维护模型可读取已有正文以避免重复")


class ReadMemoryRequest(MemoryContract):
    note_id: Identity
    start_paragraph: int = Field(default=0, ge=0, strict=True, description="续读时填写上次返回的 next_paragraph")


class NoteChange(MemoryContract):
    action: Literal["NONE", "CREATE", "UPDATE", "DEACTIVATE"]
    note_id: Identity | None = None
    expected_version: int | None = Field(default=None, strict=True, ge=1)
    document: NoteContent | None = None

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        existing = self.action in ("UPDATE", "DEACTIVATE")
        if existing != (self.note_id is not None and self.expected_version is not None):
            raise ValueError("修改或停用必须关联已读取笔记及版本")
        if not existing and (self.note_id is not None or self.expected_version is not None):
            raise ValueError("新增或无需修改不能指定旧笔记")
        if (self.action in ("CREATE", "UPDATE")) != (self.document is not None):
            raise ValueError("新增与修改必须提供完整文档")
        return self
