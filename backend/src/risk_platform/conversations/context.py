"""当前会话的压缩和持久化边界；不提取长期记忆，不执行入口动作。"""
import hashlib
from sqlalchemy import select, text
from risk_platform.core.token_budget import ContextBudgetExceeded, estimate_tokens
from . import agent as model_inputs
from . import repository, action_repository
from .contracts import ConversationSummaryDraft
from .models import ConversationSummaryRow


class ConversationCompactionError(RuntimeError):
    """未取得可靠的新摘要，当前批次的覆盖边界不推进。"""


async def latest_summary(session, conversation_id, actor):
    row = await session.scalar(select(ConversationSummaryRow).where(
        ConversationSummaryRow.conversation_id == conversation_id).order_by(
        ConversationSummaryRow.version.desc()).limit(1))
    if row is not None:
        if row.owner_id != actor:
            raise PermissionError("会话摘要不可访问")
        if hashlib.sha256(row.content.encode()).hexdigest() != row.content_digest:
            raise repository.ChatConflictError("会话摘要完整性不一致")
    return row


def history_messages(turns):
    return [message for turn in turns for message in (
        {"role":"user","content":turn["user"],"turn_number":turn["turn_number"]},
        *turn.get("read_messages", []),
        {"role":"assistant","content":turn["assistant"]})]

# 读取旧摘要与待处理历史
#     ↓
# 计算本次可用预算
#     ↓
# 输入足够小？──是──→ 直接返回，不调用摘要模型
#     ↓ 否
# 选择较早的完整轮次，组成一个批次
#     ↓
# 模型生成：旧摘要＋本批旧轮次 → 新摘要
#     ↓
# 检查格式、长度，以及是否确实缩小输入
#     ↓
# 仍然太大？──是──→ 继续压缩下一批
#     ↓ 否
# 事务保存新摘要＋覆盖边界
#     ↓
# 返回剩余历史和新摘要
async def compact_history(sessions, *, actor, turn_id, request, agent, authorize,
                          previous, turns, memory_index, compaction_budget, reserve_tokens=0, history_reader=False):
    """只压缩完整旧轮次；模型等待在事务外，全部批次成功后统一提交摘要边界。"""
    summary = previous.content if previous is not None else None
    version = previous.version if previous is not None else 0
    available = model_inputs.available_input_tokens(model_inputs.CHAT_OUTPUT_TOKENS) - reserve_tokens
    summary_available = model_inputs.available_input_tokens(model_inputs.SUMMARY_OUTPUT_TOKENS)

    def input_size(content, remaining):
        messages, tools = model_inputs.entry_request(request.message, history=history_messages(remaining),
            summary=content, memory_index=memory_index,
            investigation_id=request.investigation_id, model_id=request.model_id, history_reader=history_reader)
        return model_inputs.request_tokens(messages, tools)

    if input_size(None, []) > available:
        raise ContextBudgetExceeded("本次消息与必要工具声明已超过预算，不能通过压缩历史解决")
    through = None
    for _ in range(8):
        size = input_size(summary, turns)
        # 预留增长空间；不足两轮时不为软阈值强行压缩，但硬上限始终检查。
        if size <= int(available * .85) or (len(turns) <= 2 and size <= available):
            break
        if through is not None:
            async with sessions() as session:
                await action_repository.owned_turn(session, turn_id, actor=actor, authorize=authorize)
# 旧摘要＋选中的旧轮次 → 模型生成新摘要
# 未选中的近期轮次    → 继续保留原文
        keep = min(2, len(turns))
        if size > available and len(turns) <= 2:
            keep = max(0, len(turns) - 1)
        candidates = turns[:len(turns) - keep]
        batch = []
        for turn in candidates:
            messages = model_inputs.summary_request(summary, [*batch, turn])
            if model_inputs.request_tokens(messages) > summary_available:
                break
            batch.append(turn)
        if not batch:
            raise ContextBudgetExceeded("无法在摘要输入预算内保留完整轮次，请缩小输入")
        if compaction_budget["remaining"] <= 0:
            raise ContextBudgetExceeded("本次请求的累计摘要次数已耗尽")
        compaction_budget["remaining"] -= 1
        try:
            content = await agent.summarize(summary, batch)
            content = ConversationSummaryDraft(summary=content).summary
            if estimate_tokens(content) > model_inputs.SUMMARY_OUTPUT_TOKENS:
                raise ContextBudgetExceeded("摘要正文超过预算")
            remaining = turns[len(batch):]
            if input_size(content, remaining) >= size:
                raise ConversationCompactionError("摘要未减少输入，未保存新的覆盖边界")
        except ContextBudgetExceeded:
            raise
        except Exception as error:
            raise ConversationCompactionError("会话摘要生成失败，未推进覆盖边界") from error
        through = batch[-1]
        summary, turns = content, remaining
    if input_size(summary, turns) > available:
        raise ContextBudgetExceeded("本次会话压缩达到批次上限，未调用回答模型")
    if through is not None:
        async with sessions.begin() as session:
            # 外部调用后重新鉴权，确认原请求仍在处理，避免失权或终态请求写新摘要。
            current = await action_repository.owned_turn(session, turn_id, actor=actor, authorize=authorize, write=True)
            if current.status != "STARTED" or (request.conversation_id is not None and current.conversation_id != request.conversation_id):
                raise repository.ChatConflictError("摘要所属请求已变化或结束")
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                  {"key":"riskmind:chat:" + current.conversation_id})
            latest = await latest_summary(session, current.conversation_id, actor)
            if (latest.version if latest else 0) != version:
                raise repository.ChatConflictError("会话摘要已变化，请重新读取")
            if not (latest.through_turn_number if latest else 0) < through["turn_number"] < current.turn_number:
                raise repository.ChatConflictError("摘要覆盖边界不属于此前轮次")
            session.add(ConversationSummaryRow(conversation_id=current.conversation_id, owner_id=actor,
                version=version+1, through_turn_id=through["turn_id"], through_turn_number=through["turn_number"],
                generated_by_turn_id=turn_id, content=content, content_digest=hashlib.sha256(content.encode()).hexdigest()))
    return history_messages(turns), summary
