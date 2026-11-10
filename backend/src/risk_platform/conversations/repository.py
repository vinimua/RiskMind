"""聊天短事务操作；不在数据库事务内等待模型或运行调查。"""
import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select
from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
from risk_platform.memory.contracts import MemoryRecord, MemoryScope
from .contracts import ChatResponse
from .models import ConversationRow, ChatTurnRow


class ChatConflictError(ValueError):
    pass


class ChatBusyError(RuntimeError):
    pass


class ChatNotFoundError(LookupError):
    pass


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def authorize(callback, actor, action, resource):
    try:
        permitted = callback(actor, action, resource) is True
    except Exception:
        permitted = False
    if not permitted:
        raise PermissionError("当前身份没有所请求的会话权限")


def read_response(row):
    value = ChatResponse.model_validate_json(row.response_payload)
    if (input_digest(value) != row.response_digest or value.turn_id != row.turn_id
            or value.conversation_id != row.conversation_id or value.status != row.status):
        raise ChatConflictError("聊天结果身份或摘要不一致")
    return value


async def claim_turn(session, request, *, actor, authorize_conversation):
    turn_id = stable_id("turn-", actor, request.request_id)
    conversation_id = request.conversation_id or stable_id("chat-", actor, request.request_id)
    # 同会话首次建立和 claim 使用同一事务锁；等待模型时不持有该事务。
    from sqlalchemy import text
    # 请求键先串行化，覆盖相同 request_id 被并发投向不同会话的冲突。
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "riskmind:chat-turn:"+turn_id})
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "riskmind:chat:"+conversation_id})
    conversation = await session.get(ConversationRow, conversation_id)
    if conversation is None:
        if request.conversation_id is not None:
            raise ChatNotFoundError("会话不可访问")
        authorize(authorize_conversation, actor, "chat:create", conversation_id)
        conversation = ConversationRow(conversation_id=conversation_id, owner_id=actor)
        session.add(conversation)
        await session.flush()
    if conversation.owner_id != actor:
        raise PermissionError("会话不可访问")
    authorize(authorize_conversation, actor, "chat:read", conversation_id)
    authorize(authorize_conversation, actor, "chat:send", conversation_id)
    existing = await session.get(ChatTurnRow, turn_id)
    if existing is not None:
        if (existing.actor_id != actor or existing.conversation_id != conversation_id
                or existing.request_digest != input_digest(request)):
            raise ChatConflictError("同一请求身份对应不同消息或动作")
        if existing.status == "STARTED":
            raise ChatBusyError("本轮正在处理或上次处理结果不确定，不能重复执行")
        return conversation_id, turn_id, read_response(existing)
    active = await session.scalar(select(ChatTurnRow.turn_id).where(
        ChatTurnRow.conversation_id == conversation_id, ChatTurnRow.status == "STARTED"))
    if active is not None:
        raise ChatBusyError("同一会话已有未结束请求")
    session.add(ChatTurnRow(turn_id=turn_id, conversation_id=conversation_id, actor_id=actor,
        request_digest=input_digest(request), action=request.action, user_text=request.message, status="STARTED"))
    await session.flush()
    return conversation_id, turn_id, None


async def history_and_memories(session, conversation_id, actor):
    rows = list(await session.scalars(select(ChatTurnRow).where(
        ChatTurnRow.conversation_id == conversation_id, ChatTurnRow.actor_id == actor,
        ChatTurnRow.status == "SUCCEEDED", ChatTurnRow.action == "CHAT").order_by(ChatTurnRow.created_at.desc(), ChatTurnRow.turn_id).limit(10)))
    history, memories = [], []
    for row in reversed(rows):
        response = read_response(row)
        # 当前仅近期窗口和字符限制；不声称已经实现全 Prompt token 预算。
        history.extend(({"role": "user", "content": row.user_text[:2000]},
                        {"role": "assistant", "content": response.answer[:2000]}))
        if row.memory_payload is not None:
            memory = MemoryRecord.model_validate_json(row.memory_payload)
            if (input_digest(memory) != row.memory_digest or memory.source_id != row.turn_id
                    or memory.source_type != "CHAT_SESSION"
                    or memory.scope != MemoryScope(kind="CONVERSATION", id=conversation_id)):
                raise ChatConflictError("会话记忆来源或摘要不一致")
            memories.append(memory)
    # 历史时间顺序给模型，记忆近期优先给组装器。
    return history, list(reversed(memories))


async def complete_turn(session, response, *, memory=None):
    row = await session.scalar(select(ChatTurnRow).where(ChatTurnRow.turn_id == response.turn_id).with_for_update())
    if row is None or row.status != "STARTED" or row.conversation_id != response.conversation_id:
        raise ChatConflictError("本轮已结算或身份不一致")
    if memory is not None and (response.status != "SUCCEEDED" or memory.source_id != row.turn_id
            or memory.source_type != "CHAT_SESSION" or memory.scope != MemoryScope(kind="CONVERSATION", id=row.conversation_id)):
        raise ChatConflictError("记忆来源不属于本轮")
    row.status, row.response_payload, row.response_digest = response.status, canonical_json(response), input_digest(response)
    row.memory_payload = canonical_json(memory) if memory is not None else None
    row.memory_digest = input_digest(memory) if memory is not None else None
    row.ended_at = datetime.now(timezone.utc)
    await session.flush()
