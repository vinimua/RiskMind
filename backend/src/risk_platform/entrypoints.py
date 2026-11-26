"""告警与聊天的应用入口。调查执行统一委托 Runner，不维护第二套图或任务循环。"""
import asyncio
import json
import math
from risk_platform.core.observability import trace_chat, tag_chat_turn
from pydantic import AwareDatetime, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from risk_platform.conversations import repository as chat_store
from risk_platform.conversations.contracts import ChatResponse
from risk_platform.memory.contracts import Identity, NonBlank
from risk_platform.memory import personal
from risk_platform.conversations import context as chat_context
from risk_platform.core.token_budget import ContextBudgetExceeded
from risk_platform.workflow.contracts import MonitoringAlert, InitialPlanningInput, UserQuery
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord
from risk_platform.workflow.persistence.models import InvestigationRow, RoundRow, AcceptedPlanRow, ResultRow
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow, InvestigationRunSelectionRow
from risk_platform.workflow.runtime import InvestigationResource, InvestigationAction
from risk_platform.workflow.access import load_runtime_context
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.run_guard import RecoveryBlockedError
from risk_platform.execution.tools import tool_definition


class AlertRequest(MonitoringAlert):
    """接收已被监测服务判定的异常；本入口不计算指标或重新定义触发阈值。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    alert_id: Identity
    model_id: Identity
    metric: NonBlank = Field(max_length=128)
    symptom: NonBlank = Field(max_length=4000)
    triggered_at: AwareDatetime


class EntryServices:
    """组合入口：身份来自 API 认证；策略、模型、工具工厂由可信应用显式注入。"""
    def __init__(self, *, sessions, runner, chat_agent, tools_for,
                 authorize_resource, authorize_conversation, timeout_seconds=30):
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.sessions, self.runner, self.chat_agent = sessions, runner, chat_agent
        self.tools_for = tools_for
        self.authorize_resource = authorize_resource
        self.authorize_conversation = authorize_conversation
        self.timeout_seconds = timeout_seconds

    def _planning_source(self, actor, **source):
        value = InitialPlanningInput(**source, available_capabilities=[])
        # tools_for 负责授权与绑定；注册中心负责确认工具真实存在，不补默认能力。
        capabilities = list(dict.fromkeys(
            tool_definition(tool.name).capability for tool in self.tools_for(actor, value)
        ))
        return InitialPlanningInput(**source, available_capabilities=capabilities)

    def _resource_permission(self, actor, action, resource):
        try:
            permitted = self.authorize_resource(actor, action, resource) is True
        except Exception:
            permitted = False
        if not permitted:
            raise PermissionError("当前身份没有案件操作权限")

    async def _create_case(self, actor, case_id, source):
        # Normalize API subclasses to the canonical persisted planning contract.
        source = InitialPlanningInput.model_validate_json(source.model_dump_json())
        model_id = source.monitoring_alert.model_id if source.monitoring_alert else source.user_query.model_id
        round_id = chat_store.stable_id("round-", case_id, 1)
        self._resource_permission(actor, "investigation:create", InvestigationResource(case_id, round_id, model_id))
        async with self.sessions.begin() as session:
            # 创建与重复提交同一案件串行化，避免两名创建者覆盖 created_by。
            await ledger._lock(session, "entry-case:"+case_id)
            existing = await session.get(InvestigationRow, case_id)
            if existing is not None:
                record = ledger._read(existing, InvestigationRecord)
                if record.planning_input != source:
                    raise ledger.LedgerConflictError("同一触发身份对应不同调查输入")
            else:
                await ledger.register_investigation(session, InvestigationRecord(
                    investigation_id=case_id, created_by=actor, planning_input=source))
            await ledger.register_round(session, RoundRecord(investigation_id=case_id, round_id=round_id, round_number=1))
        return case_id

    async def _load_runtime(self, session, actor, case_id, *, execute):
        row = await session.get(InvestigationRow, case_id)
        first_round = await session.scalar(select(RoundRow).where(
            RoundRow.investigation_id == case_id, RoundRow.round_number == 1))
        if row is None or first_round is None:
            raise chat_store.ChatNotFoundError("案件不可访问")
        record = ledger._read(row, InvestigationRecord)
        tools = tuple(self.tools_for(actor, record.planning_input)) if execute else ()
        return await load_runtime_context(session, investigation_id=case_id, round_id=first_round.round_id,
            actor_id=actor, tools=tools, authorize_resource=self.authorize_resource,
            action=InvestigationAction.CONTINUE if execute else InvestigationAction.READ,
            timeout_seconds=self.timeout_seconds)

    async def _runtime(self, actor, case_id, *, execute):
        async with self.sessions() as session:
            return await self._load_runtime(session, actor, case_id, execute=execute)

    async def _run_case(self, actor, case_id):
        runtime = await self._runtime(actor, case_id, execute=True)
        return await self.runner.ainvoke(context=InvestigationInvocation(runtime=runtime))

    async def inspect_action(self, action_id, *, actor):
        from risk_platform.execution import actions
        from dataclasses import replace
        from risk_platform.workflow.persistence.models import ControlledActionRow
        from risk_platform.workflow.runtime import check_runtime
        async with self.sessions() as session:
            row = await session.get(ControlledActionRow, action_id)
            if row is None:
                raise chat_store.ChatNotFoundError("Action not found")
            case_id = actions._body(row)["investigation_id"]
        runtime = await self._runtime(actor, case_id, execute=False)
        runtime = replace(runtime, tools=tuple(self.tools_for(actor, runtime.investigation.planning_input)))
        check_runtime(runtime, InvestigationAction.READ)
        return await actions.inspect(self.sessions, action_id, runtime=runtime)

    async def confirm_action(self, action_id, decision, *, actor):
        from risk_platform.execution import actions
        view = await actions.decide(self.sessions, action_id, actor=actor, authorize=self.authorize_resource,
            digest=decision.digest, approved=decision.approved)
        # 批准者只授予批准事实；原申请者仍必须有当前执行权限。
        return await self._run_case(view["requested_by"], view["investigation_id"])

    async def resolve_action(self, action_id, resolution, *, actor):
        from risk_platform.execution import actions
        view = await actions.resolve(self.sessions, action_id, actor=actor,
            authorize=self.authorize_resource, resolution=resolution)
        return await self._run_case(view["requested_by"], view["investigation_id"])

    async def resume_investigation(self, case_id, *, actor):
        return await self._run_case(actor, case_id)

    async def download_report(self, action_id, *, actor):
        import hashlib
        from risk_platform.execution import report_export
        view = await self.inspect_action(action_id, actor=actor)
        if view["tool_name"] != report_export.ReportExportTool.name or view["status"] != "SUCCEEDED":
            raise ledger.LedgerConflictError("Report is not ready")
        from dataclasses import replace
        from risk_platform.workflow.runtime import check_runtime
        runtime = await self._runtime(actor, view["investigation_id"], execute=False)
        runtime = replace(runtime, tools=tuple(self.tools_for(actor, runtime.investigation.planning_input)))
        check_runtime(runtime, InvestigationAction.READ)
        from risk_platform.execution.actions import _authorize
        _authorize(actor, "action:download", view, self.authorize_resource)
        receipt = report_export.ReportReceipt.model_validate(view["result"]["payload"])
        path = report_export.artifact_path(report_export.binding(runtime).directory, action_id)
        content = await asyncio.to_thread(path.read_bytes)
        approved_hash = hashlib.sha256(view["material"]["markdown"].encode()).hexdigest()
        if receipt.artifact_id != action_id or hashlib.sha256(content).hexdigest() != receipt.sha256 or receipt.sha256 != approved_hash:
            raise ledger.LedgerIntegrityError("Report content differs from approved material")
        return content

    async def from_alert(self, request, *, actor):
        actor = TypeAdapter(Identity).validate_python(actor)
        request = AlertRequest.model_validate_json(request.model_dump_json())
        source = self._planning_source(actor, monitoring_alert=request)
        case_id = chat_store.stable_id("case-alert-", request.alert_id)
        await self._create_case(actor, case_id, source)
        result = await self._run_case(actor, case_id)
        return {"investigation_id": case_id, **result}

    async def _progress(self, actor, case_id):
        async with self.sessions() as session:
            latest = await session.scalar(select(RoundRow).where(RoundRow.investigation_id == case_id)
                                          .order_by(RoundRow.round_number.desc()))
            if latest is None:
                raise chat_store.ChatNotFoundError("案件不可访问")
            runtime = await load_runtime_context(session, investigation_id=case_id, round_id=latest.round_id,
                actor_id=actor, authorize_resource=self.authorize_resource, action=InvestigationAction.READ)
            plan = await session.scalar(select(AcceptedPlanRow).where(AcceptedPlanRow.round_id == runtime.round.round_id))
            run = await session.scalar(select(InvestigationRunRow).where(InvestigationRunRow.round_id == runtime.round.round_id))
            statuses, observation = [], "尚未形成"
            if run is not None:
                rows = list(await session.scalars(select(ResultRow).join(InvestigationRunSelectionRow,
                    InvestigationRunSelectionRow.result_id == ResultRow.result_id).where(
                    InvestigationRunSelectionRow.run_id == run.run_id)))
                for row in rows:
                    artifact = ledger.read_artifact(row)
                    if artifact.execution is not None:
                        statuses.append(f"{artifact.execution.task_id}: {artifact.execution.status.value}")
                    if artifact.observation is not None:
                        observation = artifact.observation.status
            # 仅输出进度，不把知识正文或历史 finding 绕过工具范围授权传给聊天。
            from risk_platform.workflow.persistence.models import RoundProgressionRow
            from risk_platform.workflow.persistence.contracts import RoundProgression
            progression = await session.get(RoundProgressionRow, runtime.round.round_id)
            reason = ledger._read(progression, RoundProgression).stop_reason if progression else None
            from risk_platform.workflow.persistence.models import ControlledActionRow
            pending = list(await session.scalars(select(ControlledActionRow).where(
                ControlledActionRow.plan_id == plan.plan_id, ControlledActionRow.result_id.is_(None)))) if plan else []
            awaiting = "；待处理动作：" + "、".join(f"{row.action_id}（{row.status}）" for row in pending) if pending else ""
            return (f"案件 {case_id} 仍为 OPEN。正式计划：{'已接受' if plan else '尚未接受'}；执行状态：{'、'.join(statuses) or '暂无已提交结果'}；本轮解释：{observation}。不表示已确认根因或关闭案件。"
                    + (f"停止原因：{reason}" if reason else "") + awaiting)
    @trace_chat
    async def chat(self, request, *, actor):
        """一次模型决策；查询立即执行，启动调查等待确认。"""
        view = await self.prepare_entry(request, actor=actor)
        if view.status == "READY":
            return await self.execute_entry(view.turn_id, actor=actor)
        return view

    async def confirm_chat(self, turn_id, confirmation, *, actor):
        """确认提交后执行同一次调用；取消不执行。"""
        view = await self.confirm_entry(turn_id, confirmation, actor=actor)
        if view.status == "READY":
            return await self.execute_entry(turn_id, actor=actor)
        return view

    async def prepare_entry(self, request, *, actor):
        from risk_platform.conversations import action_repository as actions
        from risk_platform.conversations.contracts import ToolInvocation
        from risk_platform.conversations.tools import validate_entry_target, EntryTargetError
        actor = TypeAdapter(Identity).validate_python(actor)
# 同一个 request_id 再次提交时：
# - 输入不同：拒绝。
# - 已有保存结果：返回已有状态。
# - 原模型调用还没有可靠保存：提示处理中，避免重复调用模型。
        async with self.sessions.begin() as session:
            turn, existing = await actions.begin(session, request, actor=actor, authorize=self.authorize_conversation)
            turn_id, conversation_id = turn.turn_id, turn.conversation_id
            tag_chat_turn(conversation_id, turn_id, "ENTRY_DECISION", cached=existing is not None)
            if existing is not None:
                await self._authorize_entry_result(session, actor, existing)
                return existing
        # 模型调用发生在已提交用户消息之后，不持有事务或行锁。
        try:
            compaction_budget = {"remaining":8}
            async with self.sessions() as session:
                await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation)
                previous = await chat_context.latest_summary(session, conversation_id, actor)
                turns = (await chat_store.history_turns(session, conversation_id, actor,
                    after_turn=previous.through_turn_number if previous else 0, include_reads=True))["turns"]
                memory_index = await personal.catalogue(session, actor, for_model=True)
            # 先提供个人笔记目录；正文由本次模型按需读取，身份不进入模型参数。
            async def read_memory(note_id):
                async with self.sessions() as session:
                    await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation)
                    return await personal.read_note(session, actor, note_id, for_model=True)
            async def record_read(messages):
                async with self.sessions.begin() as session:
                    turn = await actions.owned_turn(session, turn_id, actor=actor,
                        authorize=self.authorize_conversation, write=True)
                    if turn.status != "STARTED":
                        raise chat_store.ChatConflictError("已结束的轮次不能追加读取")
                    # 模型等待后重新检查读取开关和笔记版本，失权材料不写入本轮。
                    value = json.loads(messages[1]["content"])
                    note_value = value if messages[0]["tool_calls"][0]["function"]["name"] == "read_memory" else value.get("note")
                    if note_value is not None:
                        note = await personal.read_note(session, actor, note_value["note_id"], for_model=True)
                        if not value.get("historical_not_current") and note.version != note_value["version"]:
                            raise chat_store.ChatConflictError("读取期间笔记已更新")
                    await chat_store.append_read(session, turn_id, messages)
            async def read_history(from_turn, to_turn, budget, read_interaction, start_paragraph):
                from risk_platform.core.token_budget import request_tokens, paragraph_page
                async with self.sessions() as session:
                    current = await actions.owned_turn(session, turn_id, actor=actor,
                        authorize=self.authorize_conversation)
                    if to_turn >= current.turn_number:
                        raise ValueError("只能回查当前请求之前的历史")
                    if read_interaction is not None:
                        from risk_platform.conversations.models import ChatTurnRow
                        old_turn = await session.scalar(select(ChatTurnRow).where(
                            ChatTurnRow.conversation_id == conversation_id, ChatTurnRow.actor_id == actor,
                            ChatTurnRow.turn_number == from_turn))
                        pairs = await chat_store.read_interactions(session, old_turn.turn_id) if old_turn else []
                        if read_interaction >= len(pairs):
                            raise ValueError("历史读取记录不存在")
                        pair = pairs[read_interaction]
                        if pair[0]["tool_calls"][0]["function"]["name"] != "read_memory":
                            raise ValueError("此条不是笔记读取，请直接按轮次回查问答")
                        note = json.loads(pair[1]["content"])
                        await personal.read_note(session, actor, note["note_id"], for_model=True)
                        # 使用当时保存的正文，不用当前新版冒充历史依据。
                        note.update(snapshot_source_start=note["start_paragraph"],
                                    snapshot_source_next=note["next_paragraph"])
                        note.update(paragraph_page(note["content"], start=start_paragraph,
                                                   max_tokens=max(1,budget-256)))
                        return dict(from_turn=from_turn, to_turn=to_turn, historical_not_current=True,
                                    read_interaction=read_interaction, note=note)
                    page = await chat_store.history_turns(session, conversation_id, actor,
                        after_turn=from_turn-1, limit=min(100, to_turn-from_turn+1), include_reads=True)
                selected = []
                for item in page["turns"]:
                    if item["turn_number"] > to_turn:
                        break
                    if request_tokens([*selected, item]) > budget:
                        if not selected:
                            raise ContextBudgetExceeded("单个完整历史问答超过剩余预算")
                        break
                    selected.append(item)
                next_turn = selected[-1]["turn_number"]+1 if selected else page["through_turn_number"]+1
                return dict(from_turn=from_turn, to_turn=to_turn, turns=selected,
                            next_turn=next_turn if next_turn <= to_turn else None)
            async def fit_history(reserve=0):
                async with self.sessions() as session:
                    await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation)
                    latest = await chat_context.latest_summary(session, conversation_id, actor)
                    remaining = (await chat_store.history_turns(session, conversation_id, actor,
                        after_turn=latest.through_turn_number if latest else 0, include_reads=True))["turns"]
                history, summary = await chat_context.compact_history(self.sessions, actor=actor,
                    turn_id=turn_id, request=request, agent=self.chat_agent, authorize=self.authorize_conversation,
                    previous=latest, turns=remaining, memory_index=memory_index, compaction_budget=compaction_budget,
                    reserve_tokens=reserve, history_reader=True)
                from risk_platform.conversations.agent import entry_request
                return entry_request(request.message, history=history, summary=summary, memory_index=memory_index,
                    investigation_id=request.investigation_id, model_id=request.model_id, history_reader=True)[0]
            async with asyncio.timeout(self.timeout_seconds):
                history, summary = await chat_context.compact_history(self.sessions, actor=actor,
                    turn_id=turn_id, request=request, agent=self.chat_agent, authorize=self.authorize_conversation,
                    previous=previous, turns=turns, memory_index=memory_index,
                    compaction_budget=compaction_budget, history_reader=True)
                decision = await self.chat_agent.decide(request.message,
                    history=history, summary=summary, memory_index=memory_index, read_memory=read_memory,
                    investigation_id=request.investigation_id, model_id=request.model_id,
                    read_history=read_history, record_read=record_read, fit_history=fit_history)
            # 结果有两种结构：
            # - 工具调用：工具名称、参数、目标案件、目标模型。
            # - 普通回答：回答正文与可选记忆候选。
            rejection = None
            if isinstance(decision, ToolInvocation):
                try:
                    validate_entry_target(request, decision)
                except EntryTargetError as error:
                    rejection = str(error)
        except Exception as error:
            # 无可靠决策时，只保存失败结果；不生成替代动作或自动再次调用模型。
            code, answer = "DEPENDENCY", "入口决策未完成，没有执行入口工具。"
            if isinstance(error, ContextBudgetExceeded):
                code, answer = "CONTEXT_BUDGET", "会话输入超过预算，请缩小输入后使用新的 request_id 重试；原始历史已保留。"
            elif isinstance(error, chat_context.ConversationCompactionError):
                code, answer = "COMPACTION_FAILED", "会话摘要未完成，未执行入口工具；请使用新的 request_id 重试。"
            async with self.sessions.begin() as session:
                turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
                await chat_store.complete_turn(session, ChatResponse(conversation_id=conversation_id,turn_id=turn_id,
                    status="FAILED",answer=answer,error_code=code))
                return await actions.state(session, turn)
        async with self.sessions.begin() as session:
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
            await actions.save_decision(session, turn, request, decision, rejection=rejection)
            view = await actions.state(session, turn)
        from risk_platform.conversations.contracts import ChatDraft
        if isinstance(decision, ChatDraft) and decision.memory_candidate:
            try:
                async with asyncio.timeout(min(12, self.timeout_seconds)):
                    await personal.maintenance(self.sessions, actor, turn_id, self.chat_agent,
                                               timeout_seconds=min(10, self.timeout_seconds))
            except Exception:
                # 回答已提交；连维护状态都无法写回时保留候选，允许显式重试。
                pass
        return view

    async def _authorize_entry_result(self, session, actor, view):
        if view.result is not None and view.result.investigation_id is not None:
            await self._load_runtime(session, actor, view.result.investigation_id, execute=False)

    async def inspect_entry(self, turn_id, *, actor):
        """即使响应丢失，也可按原 turn 定位调用与预分配案件；不重新执行。"""
        from risk_platform.conversations import action_repository as actions
        async with self.sessions() as session:
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation)
            view = await actions.state(session, turn)
            await self._authorize_entry_result(session, actor, view)
            return view

    async def confirm_entry(self, turn_id, confirmation, *, actor):
        """确认事实来自已认证用户，不来自模型；确认本身不执行工具。"""
        from risk_platform.conversations import action_repository as actions
        from risk_platform.conversations.contracts import EntryConfirmation
        confirmation = EntryConfirmation.model_validate_json(confirmation.model_dump_json())
        async with self.sessions.begin() as session:
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
            await actions.confirm(session, turn, confirmation, actor=actor)
            view = await actions.state(session, turn)
            await self._authorize_entry_result(session, actor, view)
            return view
# turn_id：执行哪一次聊天请求对应的调用
    async def execute_entry(self, turn_id, *, actor):
        """只推进已登记、已过 Gate 的单个调用；STARTED/终态直接返回，不盲目重试。"""
        import json
        from risk_platform.conversations import action_repository as actions
        from risk_platform.conversations.models import EntryDecisionRow
        from risk_platform.conversations.contracts import ToolInvocation
        async with self.sessions.begin() as session:
#   turn = await actions.owned_turn里检查：
# - 这条聊天请求是否存在。
# - 会话和请求是否属于当前用户。
# - 用户是否仍有 chat:read、`chat:send` 权限。
# - 是否属于新的入口协议。
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
            row = await session.get(EntryDecisionRow, turn_id, with_for_update=True)
            if row is None:
                raise chat_store.ChatConflictError("入口决策尚未可靠登记")
            decision = actions.read_decision(row, turn)
            if row.status in actions.TERMINAL or row.status == "STARTED":
                view = await actions.state(session, turn)
                await self._authorize_entry_result(session, actor, view)
                return view
            if row.status != "READY" or not isinstance(decision, ToolInvocation):
                raise chat_store.ChatConflictError("入口调用尚未获得确认")

            # 聊天权限和案件权限是分开的。
            if decision.name == "get_investigation_progress":
                await self._load_runtime(session, actor, row.investigation_id, execute=False)
            else:
                resource = InvestigationResource(row.investigation_id,
                    chat_store.stable_id("round-", row.investigation_id, 1), decision.arguments.model_id)
                for action in ("investigation:create", InvestigationAction.READ.value, InvestigationAction.CONTINUE.value):
                    self._resource_permission(actor, action, resource)
            row, claimed = await actions.start(session, turn)
            if not claimed:
                raise chat_store.ChatConflictError("调用已被其他执行者领取")
            conversation_id, case_id = turn.conversation_id, row.investigation_id
            message = json.loads(row.request_payload)["message"]
        # STARTED 已提交；下面调用原服务和原 Runner，没有新任务循环。
        result_case = None
        uncertain = False
        try:
            if decision.name == "get_investigation_progress":
                # 查询案件进度：
                answer = await self._progress(actor, case_id)
                result_case = case_id
            else:
                #把用户消息和选定模型转换成调查输入。当前这里明确只提供知识检索能力
                source = self._planning_source(actor,
                    user_query=UserQuery(query=message, model_id=decision.arguments.model_id))
                result_case = await self._create_case(actor, case_id, source)
                await self._run_case(actor, case_id)
                answer = await self._progress(actor, case_id)
            response = ChatResponse(conversation_id=conversation_id, turn_id=turn_id,
                status="SUCCEEDED",answer=answer,investigation_id=result_case)
        except Exception as error:
            known = isinstance(error,(PermissionError,chat_store.ChatNotFoundError,ledger.LedgerConflictError,RecoveryBlockedError))
            code = ("FORBIDDEN" if isinstance(error,PermissionError) else "NOT_FOUND" if isinstance(error,chat_store.ChatNotFoundError)
                    else "CONFLICT" if known else "DEPENDENCY")
            uncertain = not known
            response = ChatResponse(conversation_id=conversation_id,turn_id=turn_id,status="FAILED",
                answer="本次入口调用未完成；请按原 turn 查询关联案件，不能换请求身份重复启动。",
                investigation_id=result_case,error_code=code)
        # 若此处崩溃/被取消/授权撤销，STARTED 和稳定案件关联仍在；调用者可以 inspect，不能自动重跑。
#  这里重新检查当前聊天权限，然后原子保存：
# - 动作最终状态。
# - 最终聊天响应。
# - 关联案件 ID。
# 最后 state() 读取这些记录，返回给调用方。
# 如果进程在执行中退出，或者最后保存结果失败，数据库可能仍显示 STARTED。当前函数不会自动重跑，需要通过原 turn_id 查询关联案件并核实。
        async with self.sessions.begin() as session:
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
            await actions.finish(session, turn, response, uncertain=uncertain)
            return await actions.state(session, turn)
