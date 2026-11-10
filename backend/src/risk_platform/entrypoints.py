"""告警与聊天的应用入口。调查执行统一委托 Runner，不维护第二套图或任务循环。"""
import asyncio
import math
from pydantic import AwareDatetime, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from risk_platform.conversations import repository as chat_store
from risk_platform.conversations.contracts import ChatDraft, ChatResponse
from risk_platform.memory.contracts import Identity, NonBlank, MemoryRecord, MemoryScope
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


class AlertRequest(MonitoringAlert):
    """接收已被监测服务判定的异常；本入口不计算指标或重新定义触发阈值。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    alert_id: Identity
    model_id: Identity
    metric: NonBlank = Field(max_length=128)
    symptom: NonBlank = Field(max_length=4000)
    time_window: NonBlank = Field(max_length=256)
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

    async def _runtime(self, actor, case_id, *, execute):
        async with self.sessions() as session:
            row = await session.get(InvestigationRow, case_id)
            first_round = await session.scalar(select(RoundRow).where(
                RoundRow.investigation_id == case_id, RoundRow.round_number == 1))
            if row is None or first_round is None:
                raise chat_store.ChatNotFoundError("案件不可访问")
            record = ledger._read(row, InvestigationRecord)
            # 工具依赖只在执行时由可信工厂绑定；纯进度查询不读取知识正文。
            tools = tuple(self.tools_for(actor, record.planning_input)) if execute else ()
            return await load_runtime_context(session, investigation_id=case_id, round_id=first_round.round_id,
                actor_id=actor, tools=tools, authorize_resource=self.authorize_resource,
                action=InvestigationAction.CONTINUE if execute else InvestigationAction.READ,
                timeout_seconds=self.timeout_seconds)

    async def _run_case(self, actor, case_id):
        runtime = await self._runtime(actor, case_id, execute=True)
        return await self.runner.ainvoke(context=InvestigationInvocation(runtime=runtime))

    async def from_alert(self, request, *, actor):
        actor = TypeAdapter(Identity).validate_python(actor)
        request = AlertRequest.model_validate_json(request.model_dump_json())
        source = InitialPlanningInput(monitoring_alert=request, available_capabilities=["KNOWLEDGE_RETRIEVAL"])
        case_id = chat_store.stable_id("case-alert-", request.alert_id)
        await self._create_case(actor, case_id, source)
        result = await self._run_case(actor, case_id)
        return {"investigation_id": case_id, **result}

    async def _progress(self, actor, case_id):
        runtime = await self._runtime(actor, case_id, execute=False)
        async with self.sessions() as session:
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
            return f"案件 {case_id} 仍为 OPEN。正式计划：{'已接受' if plan else '尚未接受'}；执行状态：{'、'.join(statuses) or '暂无已提交结果'}；本轮解释：{observation}。不表示已确认根因或关闭案件。"

    async def chat(self, request, *, actor):
        actor = TypeAdapter(Identity).validate_python(actor)
        async with self.sessions.begin() as session:
            conversation_id, turn_id, existing = await chat_store.claim_turn(session, request,
                actor=actor, authorize_conversation=self.authorize_conversation)
        if existing is not None:
            # 复用涉及案件的回答，也要重新确认该案件当前可读。
            if existing.investigation_id is not None:
                await self._runtime(actor, existing.investigation_id, execute=False)
            return existing
        memory, case_id = None, None
        try:
            if request.action == "READ_INVESTIGATION":
                answer = await self._progress(actor, request.investigation_id)
                case_id = request.investigation_id
            elif request.action == "START_INVESTIGATION":
                source = InitialPlanningInput(user_query=UserQuery(query=request.message, model_id=request.model_id),
                    available_capabilities=["KNOWLEDGE_RETRIEVAL"])
                case_id = await self._create_case(actor, chat_store.stable_id("case-chat-", turn_id), source)
                await self._run_case(actor, case_id)
                answer = await self._progress(actor, case_id)
            else:
                async with self.sessions() as session:
                    chat_store.authorize(self.authorize_conversation, actor, "chat:read", conversation_id)
                    history, memories = await chat_store.history_and_memories(session, conversation_id, actor)
                context = assemble_context(scope=MemoryScope(kind="CONVERSATION", id=conversation_id), candidates=memories)
                async with asyncio.timeout(self.timeout_seconds):
                    draft = await asyncio.to_thread(self.chat_agent.reply, request.message, history=history, memory_context=context)
                draft = ChatDraft.model_validate_json(draft.model_dump_json())
                answer = draft.answer
                if draft.memory.item_count():
                    memory = MemoryRecord(memory_id=chat_store.stable_id("memory-chat-", turn_id),
                        scope=MemoryScope(kind="CONVERSATION", id=conversation_id), source_type="CHAT_SESSION",
                        source_id=turn_id, content=draft.memory)
            response = ChatResponse(conversation_id=conversation_id, turn_id=turn_id, status="SUCCEEDED",
                                    answer=answer, investigation_id=case_id)
        except Exception as error:
            code = ("FORBIDDEN" if isinstance(error, PermissionError) else
                    "NOT_FOUND" if isinstance(error, chat_store.ChatNotFoundError) else
                    "CONFLICT" if isinstance(error, (ledger.LedgerConflictError, RecoveryBlockedError)) else "DEPENDENCY")
            response = ChatResponse(conversation_id=conversation_id, turn_id=turn_id, status="FAILED",
                answer="本轮未能完成，请按错误状态处理；未确认任何案件根因。", investigation_id=case_id, error_code=code)
            memory = None
        # 外部调用完成后重验会话权限，再原子保存回答与记忆；取消/进程退出保留 STARTED。
        async with self.sessions.begin() as session:
            chat_store.authorize(self.authorize_conversation, actor, "chat:read", conversation_id)
            chat_store.authorize(self.authorize_conversation, actor, "chat:send", conversation_id)
            await chat_store.complete_turn(session, response, memory=memory)
        return response
