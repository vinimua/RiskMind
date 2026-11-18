"""告警与聊天的应用入口。调查执行统一委托 Runner，不维护第二套图或任务循环。"""
import asyncio
import math
from risk_platform.core.observability import trace_chat, tag_chat_turn
from pydantic import AwareDatetime, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from risk_platform.conversations import repository as chat_store
from risk_platform.conversations.contracts import ChatResponse
from risk_platform.memory.contracts import Identity, NonBlank, MemoryScope
from risk_platform.memory.context import assemble_context
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
            return (f"案件 {case_id} 仍为 OPEN。正式计划：{'已接受' if plan else '尚未接受'}；执行状态：{'、'.join(statuses) or '暂无已提交结果'}；本轮解释：{observation}。不表示已确认根因或关闭案件。"
                    + (f"停止原因：{reason}" if reason else ""))
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
            async with self.sessions() as session:
                await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation)
                history, memories = await chat_store.history_and_memories(session, conversation_id, actor)
            context = assemble_context(scope=MemoryScope(kind="CONVERSATION", id=conversation_id), candidates=memories)
            # 模型会收到本次消息、近期聊天记录、选中的历史记忆，以及调用方明确绑定的目标。
            async with asyncio.timeout(self.timeout_seconds):
                decision = await asyncio.to_thread(self.chat_agent.decide, request.message,
                    history=history, memory_context=context, investigation_id=request.investigation_id, model_id=request.model_id)
            # 结果有两种结构：
            # - 工具调用：工具名称、参数、目标案件、目标模型。
            # - 普通回答：回答正文与可选记忆候选。
            rejection = None
            if isinstance(decision, ToolInvocation):
                try:
                    validate_entry_target(request, decision)
                except EntryTargetError as error:
                    rejection = str(error)
        except Exception:
            # 无可靠决策时，只保存失败结果；不生成替代动作或自动再次调用模型。
            async with self.sessions.begin() as session:
                turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
                await chat_store.complete_turn(session, ChatResponse(conversation_id=conversation_id,turn_id=turn_id,
                    status="FAILED",answer="入口决策未完成，没有执行入口工具。",error_code="DEPENDENCY"))
                return await actions.state(session, turn)
        async with self.sessions.begin() as session:
            turn = await actions.owned_turn(session, turn_id, actor=actor, authorize=self.authorize_conversation, write=True)
            await actions.save_decision(session, turn, request, decision, rejection=rejection)
            return await actions.state(session, turn)

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
