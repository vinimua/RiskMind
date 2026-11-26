"""分类正文与集中来源；契约只校验数据，不提供访问权限。"""
from typing import Annotated, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identity = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]
NonBlank = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]


class MemoryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryScope(MemoryContract):
    """记忆属于案件还是会话；id 的含义由 kind 决定，不是权限凭证。"""
    kind: Literal["INVESTIGATION", "CONVERSATION"]
    id: Identity


class MemoryContent(MemoryContract):
    """参考 DevF 按列表分类；列表中直接放正文，不再给每句话套 kind 和来源对象。"""
    execution_summaries: tuple[NonBlank, ...] = ()
    historical_findings: tuple[NonBlank, ...] = ()  # 案件提取时把原限制条件一起保留。
    follow_ups: tuple[NonBlank, ...] = ()  # 待确认问题与后续建议，不是正式调查任务。
    user_preferences: tuple[NonBlank, ...] = ()
    background: tuple[NonBlank, ...] = ()

    def item_count(self) -> int:
        return sum(len(getattr(self, name)) for name in type(self).model_fields)


class MemoryRecord(MemoryContract):
    """一份来源对应一组分类内容；来源只保存一次，原文通过 source_id 回查。"""
    schema_version: Literal[3] = 3
    memory_id: Identity
    scope: MemoryScope
    source_type: Literal["OBSERVATION_RESULT", "CHAT_MESSAGE", "CHAT_SESSION"]
    source_id: Identity  # 对应账本 Result、聊天消息或封存会话的 ID。
    content: MemoryContent

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        investigation = self.scope.kind == "INVESTIGATION"
        if investigation != (self.source_type == "OBSERVATION_RESULT"):
            raise ValueError("记忆归属与来源类型不一致")
        if not investigation and self.content.execution_summaries:
            raise ValueError("聊天记录不能冒充正式执行事实")
        if not self.content.item_count():
            raise ValueError("记忆必须包含内容")
        return self


class AgentContext(MemoryContract):
    """本次选中的记忆；不是全量历史，也不是可信 Runtime。"""
    schema_version: Literal[6] = 6
    scope: MemoryScope
    memories: tuple[MemoryRecord, ...] = ()
    max_chars: int = Field(default=12000, ge=512, le=100000, strict=True)

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        identities = [m.memory_id for m in self.memories]
        if len(set(identities)) != len(identities):
            raise ValueError("上下文不能重复包含同一记忆")
        if any(memory.scope != self.scope for memory in self.memories):
            raise ValueError("上下文来源与归属不一致")
        return self


class ContextSnapshot(MemoryContract):
    """把实际上下文固定到运行/阶段；来源鉴权和数据库不可变性在仓库层处理。"""
    schema_version: Literal[6] = 6
    run_id: Identity
    stage: Identity
    context: AgentContext
    
# 一次会话：conversation_id = C1
# │
# ├─ 第 1 轮：turn_id = T1
# │   用户：请结合代码解释
# │   助手：好的……
# │   保存记忆 M1：用户喜欢结合代码
# │
# ├─ 第 2 轮：turn_id = T2
# │   用户：解释 prepare_plan
# │   助手：这个函数……
# │   可以读取 M1，并产生新的记忆 M2
# │
# └─ 第 3 轮：turn_id = T3
#     用户继续提问……
# 对应到类：
# 类	对应上例中的什么
# MemoryScope	归属范围，如 kind="CONVERSATION", id="C1"，表示属于整个会话
# MemoryContent	记忆正文，如“用户喜欢结合代码”，自身不代表会话或轮次
# MemoryRecord	某一轮产生的一份记忆，例如 M1：归属 C1，来源 source_id=T1
# AgentContext	本次调用选中的记忆，例如回答 T3 时选择了 M1、M2
# ContextSnapshot	某个案件调查阶段选用了什么记忆，当前不是聊天会话或聊天轮次对象