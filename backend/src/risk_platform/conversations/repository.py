"""聊天短事务操作；不在数据库事务内等待模型或运行调查。"""
import hashlib
import json
from itertools import groupby
from datetime import datetime, timezone
from sqlalchemy import select
from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
from .contracts import ChatResponse
from .models import ConversationRow, ChatTurnRow, EntryDecisionRow, ChatReadRow
from typing import Literal
from pydantic import Field
from risk_platform.memory.contracts import MemoryContract, Identity, NonBlank


class _LegacyIntent(MemoryContract):
    """仅校验已落库 AUTO 历史，不参与执行。"""
    intent: Literal["CHAT", "READ_INVESTIGATION", "START_INVESTIGATION", "CLARIFY"]
    investigation_id: Identity | None = None
    model_id: Identity | None = None
    clarification: NonBlank | None = Field(default=None, max_length=1000)


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
        request_digest=input_digest(request), action="ENTRY_DECISION", user_text=request.message, status="STARTED"))
    await session.flush()
    return conversation_id, turn_id, None


async def history_turns(session, conversation_id, actor, *, after_turn=0, limit=None, include_reads=False):
    conversation = await session.get(ConversationRow, conversation_id)
    if conversation is None or conversation.owner_id != actor:
        raise ChatNotFoundError("会话不可访问")
    statement = select(ChatTurnRow, EntryDecisionRow).outerjoin(EntryDecisionRow,
        EntryDecisionRow.turn_id == ChatTurnRow.turn_id).where(
        ChatTurnRow.conversation_id == conversation_id, ChatTurnRow.actor_id == actor,
        ChatTurnRow.turn_number > after_turn, ChatTurnRow.status == "SUCCEEDED",
        ChatTurnRow.action.in_(("CHAT", "AUTO", "ENTRY_DECISION"))).order_by(ChatTurnRow.turn_number)
    if limit is not None:
        statement = statement.limit(limit)
    rows = list((await session.execute(statement)).all())
    turns = []
    readable = {}
    read_groups = {}
    if include_reads:
        from risk_platform.memory import personal
        readable = {note["note_id"]:note for note in await personal.catalogue(session, actor, for_model=True)}
        read_rows = list(await session.scalars(select(ChatReadRow).where(
            ChatReadRow.turn_id.in_([row.turn_id for row, _ in rows])).order_by(ChatReadRow.turn_id, ChatReadRow.sequence)))
        read_groups = {identity:_read_pairs(list(group)) for identity, group in groupby(read_rows, key=lambda r:r.turn_id)}
    for row, decision_row in rows:
        response = read_response(row)
        if row.action == "ENTRY_DECISION":
            from .action_repository import read_decision
            from .contracts import ChatDraft
            if decision_row is None:
                raise ChatConflictError("新入口回答缺少可靠决策")
            if not isinstance(read_decision(decision_row, row), ChatDraft):
                continue  # 工具结果不通过聊天历史绕过案件鉴权。
        if row.action == "AUTO":
            if row.intent_payload is None:
                raise ChatConflictError("自动路由结果缺少意图记录")
            intent = _LegacyIntent.model_validate_json(row.intent_payload)
            if input_digest(intent) != row.intent_digest:
                raise ChatConflictError("意图记录摘要不一致")
            if intent.intent not in ("CHAT", "CLARIFY"):
                continue  # 案件结果不能经普通聊天历史绕过案件 READ 授权。
        turn = dict(turn_id=row.turn_id, turn_number=row.turn_number,
                    user=row.user_text, assistant=response.answer)
        if include_reads:
            pairs = read_groups.get(row.turn_id, [])
            read_messages = []
            for interaction, pair in enumerate(pairs):
                value = json.loads(pair[1]["content"])
                if pair[0]["tool_calls"][0]["function"]["name"] == "read_memory":
                    note_id = value["note_id"]
                    note = readable.get(note_id)
                    if note is None:
                        continue  # 失权或停用笔记不带入后续聊天。
                    # 历史笔记正文不转存成永久聊天摘要；后续需要时重新读当前有效笔记。
                    value = {k: value[k] for k in ("note_id", "version", "start_paragraph", "next_paragraph") if k in value}
                    value.update(body_omitted=True, current_version=note["version"],
                        source_turn=row.turn_number, read_interaction=interaction,
                        recovery="当前偏好用 read_memory；核对当时依据用 read_chat_history 回查此轮读取序号")
                    pair = [pair[0], dict(pair[1], content=json.dumps(value, ensure_ascii=False))]
                else:
                    value = {"body_omitted":True, "recovery":pair[0]["tool_calls"][0]["function"]}
                    pair = [pair[0], dict(pair[1], content=json.dumps(value, ensure_ascii=False))]
                read_messages.extend(pair)
            if read_messages:
                turn["read_messages"] = read_messages
        turns.append(turn)
    return {"turns": turns, "through_turn_number": rows[-1][0].turn_number if rows else after_turn}


async def complete_turn(session, response):
    row = await session.scalar(select(ChatTurnRow).where(ChatTurnRow.turn_id == response.turn_id).with_for_update())
    if row is None or row.status != "STARTED" or row.conversation_id != response.conversation_id:
        raise ChatConflictError("本轮已结算或身份不一致")
    row.status, row.response_payload, row.response_digest = response.status, canonical_json(response), input_digest(response)
    row.ended_at = datetime.now(timezone.utc)
    await session.flush()


async def read_interactions(session, turn_id):
    """调用方负责轮次归属；校验顺序与原始内容后返回完整读取配对。"""
    rows = list(await session.scalars(select(ChatReadRow).where(
        ChatReadRow.turn_id == turn_id).order_by(ChatReadRow.sequence)))
    return _read_pairs(rows)


def _read_pairs(rows):
    """单轮查询与历史批量查询共用同一顺序、摘要校验。"""
    values = []
    for sequence, row in enumerate(rows):
        if row.sequence != sequence or hashlib.sha256(row.payload.encode()).hexdigest() != row.digest:
            raise ChatConflictError("读取交互顺序或正文摘要不一致")
        values.append(json.loads(row.payload))
    return values


async def append_read(session, turn_id, messages):
    """在持有本轮行锁的短事务中调用，完整保存请求和结果，不保存客户端。"""
    rows = await read_interactions(session, turn_id)
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    session.add(ChatReadRow(turn_id=turn_id, sequence=len(rows), payload=payload,
                           digest=hashlib.sha256(payload.encode()).hexdigest()))
