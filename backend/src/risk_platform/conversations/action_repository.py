"""聊天入口决策和调用的短事务操作；不运行模型、工具或调查图。"""
import hashlib
import json
from datetime import datetime, timezone
from pydantic import TypeAdapter
from sqlalchemy import select, text
from risk_platform.memory.contracts import MemoryRecord, MemoryScope
from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
from . import repository as chat
from .contracts import ChatDraft, ToolInvocation, ChatResponse, EntryActionState
from .models import ConversationRow, ChatTurnRow, EntryDecisionRow

DECISION = TypeAdapter(ChatDraft | ToolInvocation)
TERMINAL = {"ANSWERED", "REJECTED", "SUCCEEDED", "FAILED", "UNCERTAIN", "CANCELLED"}


async def owned_turn(session, turn_id, *, actor, authorize, write=False):
    query = select(ChatTurnRow).where(ChatTurnRow.turn_id == turn_id)
    if write:
        query = query.with_for_update()
    turn = await session.scalar(query)
    if turn is None:
        raise chat.ChatNotFoundError("聊天请求不可访问")
    conversation = await session.get(ConversationRow, turn.conversation_id)
    if conversation is None or conversation.owner_id != actor or turn.actor_id != actor:
        raise PermissionError("聊天请求不可访问")
    chat.authorize(authorize, actor, "chat:read", turn.conversation_id)
    if write:
        chat.authorize(authorize, actor, "chat:send", turn.conversation_id)
    if turn.action != "ENTRY_DECISION":
        raise chat.ChatConflictError("该 turn 不属于新入口决策协议")
    return turn


def read_decision(row, turn):
    if hashlib.sha256(row.request_payload.encode()).hexdigest() != turn.request_digest:
        raise chat.ChatConflictError("决策原始请求摘要不一致")
    value = DECISION.validate_json(row.decision_payload)
    if input_digest(value) != row.decision_digest:
        raise chat.ChatConflictError("决策摘要不一致")
    if isinstance(value, ToolInvocation):
        expected = chat.stable_id("entry-call-", turn.turn_id)
        if row.invocation_id != expected:
            raise chat.ChatConflictError("调用身份不属于原 turn")
        case_id = (value.arguments.investigation_id if value.name == "get_investigation_progress"
                   else chat.stable_id("case-chat-", turn.turn_id))
        if row.investigation_id != case_id:
            raise chat.ChatConflictError("案件身份不属于原决策")
    elif row.invocation_id is not None:
        raise chat.ChatConflictError("普通回答不能绑定工具调用")
    return value


async def state(session, turn):
    row = await session.get(EntryDecisionRow, turn.turn_id)
    result = chat.read_response(turn) if turn.status != "STARTED" else None
    if row is None and turn.status == "SUCCEEDED":
        raise chat.ChatConflictError("成功入口请求缺少可靠决策")
    return EntryActionState(turn_id=turn.turn_id, conversation_id=turn.conversation_id,
        status=row.status if row else ("DECIDING" if result is None else "FAILED"),
        decision=read_decision(row, turn) if row else None,
        decision_digest=row.decision_digest if row else None,
        invocation_id=row.invocation_id if row else None,
        investigation_id=row.investigation_id if row else None, result=result)


async def begin(session, request, *, actor, authorize):
    turn_id = chat.stable_id("turn-", actor, request.request_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                          {"key": "riskmind:chat-turn:" + turn_id})
    if await session.get(ChatTurnRow, turn_id) is not None:
        turn = await owned_turn(session, turn_id, actor=actor, authorize=authorize, write=True)
        if turn.request_digest != input_digest(request):
            raise chat.ChatConflictError("同一请求身份对应不同输入")
        view = await state(session, turn)
        if view.status == "DECIDING":
            raise chat.ChatBusyError("决策尚未可靠提交，禁止重复调用模型")
        return turn, view
    _, _, _ = await chat.claim_turn(session, request, actor=actor,
        authorize_conversation=authorize)
    return await session.get(ChatTurnRow, turn_id), None


async def save_decision(session, turn, request, value, *, rejection=None):
    if turn.status != "STARTED" or await session.get(EntryDecisionRow, turn.turn_id) is not None:
        raise chat.ChatConflictError("决策不能重复写入或覆盖")
    value = DECISION.validate_json(value.model_dump_json())
    if input_digest(request) != turn.request_digest:
        raise chat.ChatConflictError("决策输入不能替换原始消息")
    call = isinstance(value, ToolInvocation)
    case_id = None
    if call:
        case_id = (value.arguments.investigation_id if value.name == "get_investigation_progress"
                   else chat.stable_id("case-chat-", turn.turn_id))
    status = ("REJECTED" if rejection else "PENDING_CONFIRMATION" if call and value.name == "start_investigation"
              else "READY" if call else "ANSWERED")
    row = EntryDecisionRow(turn_id=turn.turn_id, request_payload=canonical_json(request),
        decision_payload=canonical_json(value), decision_digest=input_digest(value), status=status,
        invocation_id=chat.stable_id("entry-call-", turn.turn_id) if call else None, investigation_id=case_id)
    session.add(row)
    await session.flush()  # 先登记可靠决策，最终回答随后在同一事务提交。
    if rejection or not call:
        response = ChatResponse(conversation_id=turn.conversation_id, turn_id=turn.turn_id,
            status="SUCCEEDED", answer=rejection or value.answer)
        memory = None
        if not call and value.memory.item_count():
            memory = MemoryRecord(memory_id=chat.stable_id("memory-chat-", turn.turn_id),
                scope=MemoryScope(kind="CONVERSATION", id=turn.conversation_id),
                source_type="CHAT_SESSION", source_id=turn.turn_id, content=value.memory)
        await chat.complete_turn(session, response, memory=memory)
    await session.flush()
    return row


async def confirm(session, turn, confirmation, *, actor):
    row = await session.get(EntryDecisionRow, turn.turn_id, with_for_update=True)
    if row is None:
        raise chat.ChatConflictError("没有已保存的调用")
    value = read_decision(row, turn)
    if (not isinstance(value, ToolInvocation) or value.name != "start_investigation"
            or confirmation.invocation_id != row.invocation_id or confirmation.decision_digest != row.decision_digest):
        raise chat.ChatConflictError("确认对象不属于已保存的启动请求")
    if row.confirmed is not None:
        if row.confirmed != confirmation.confirmed or row.confirmation_actor != actor:
            raise chat.ChatConflictError("确认结果不可修改")
        return row
    if row.status != "PENDING_CONFIRMATION":
        raise chat.ChatConflictError("当前状态不能确认")
    row.confirmed, row.confirmation_actor, row.confirmed_at = confirmation.confirmed, actor, datetime.now(timezone.utc)
    row.status = "READY" if confirmation.confirmed else "CANCELLED"
    if not confirmation.confirmed:
        await chat.complete_turn(session, ChatResponse(conversation_id=turn.conversation_id,turn_id=turn.turn_id,
            status="SUCCEEDED", answer="已取消本次调查启动请求；没有执行调查。"))
    await session.flush()
    return row


async def start(session, turn):
    row = await session.get(EntryDecisionRow, turn.turn_id, with_for_update=True)
    if row is None:
        raise chat.ChatConflictError("尚无可靠决策")
    value = read_decision(row, turn)
    if row.status in TERMINAL or row.status == "STARTED":
        return row, False
    if row.status != "READY" or not isinstance(value, ToolInvocation):
        raise chat.ChatConflictError("调用尚未获得确认或不可执行")
    if value.name == "start_investigation" and row.confirmed is not True:
        raise chat.ChatConflictError("启动调查必须显式确认")
    row.status, row.started_at = "STARTED", datetime.now(timezone.utc)
    await session.flush()
    return row, True


async def finish(session, turn, response, *, uncertain=False):
    row = await session.get(EntryDecisionRow, turn.turn_id, with_for_update=True)
    if row is None or row.status != "STARTED":
        raise chat.ChatConflictError("只有开始执行的调用可以结算")
    read_decision(row, turn)
    if response.turn_id != turn.turn_id or response.conversation_id != turn.conversation_id:
        raise chat.ChatConflictError("工具结果不属于原 turn")
    if response.investigation_id is not None and response.investigation_id != row.investigation_id:
        raise chat.ChatConflictError("工具结果不能绑定其他案件")
    if uncertain and response.status != "FAILED":
        raise chat.ChatConflictError("不确定结果不能伪装成成功")
    row.status = "UNCERTAIN" if uncertain else response.status
    await chat.complete_turn(session, response)
    await session.flush()
