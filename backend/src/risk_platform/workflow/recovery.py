"""首轮持久化执行：图内规划、正式接受、阶段入账与恢复；节点推进仍由 LangGraph 决定。"""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from risk_platform.agents.prompts import INITIAL_PLANNING_SYSTEM_PROMPT, KNOWLEDGE_OBSERVATION_SYSTEM_PROMPT, KNOWLEDGE_QUERY_SYSTEM_PROMPT
from risk_platform.execution.contracts import ExecutionError, KnowledgeSearchToolRequest, TaskExecutionResult
from risk_platform.workflow import access, nodes
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.observation import RoundObservation
from risk_platform.workflow.runtime import InvestigationAction, InvestigationRuntimeContext, check_runtime
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.business_inputs import BusinessOperationInput
from risk_platform.workflow.persistence.contracts import AcceptedPlan, OperationType, canonical_json, input_digest, operation_key
from risk_platform.workflow.persistence.models import AcceptedPlanRow, AttemptRow, ResultRow
from risk_platform.workflow.persistence.recovery_contracts import FirstRoundSpec, PhaseArtifact, RecoveryPolicy, RunClaim
from risk_platform.workflow.persistence.planning_contracts import PlanningSpec, PlanningCandidate
from risk_platform.workflow.persistence.planning_models import PlanningRunRow, PlanningAcceptanceRow
from risk_platform.workflow.persistence import planning_repository as planning
from risk_platform.workflow.persistence.recovery_models import (
    InvestigationRunAttemptRow, InvestigationRunClaimRow, InvestigationRunRow, InvestigationRunSelectionRow,
)


class RecoveryBlockedError(RuntimeError):
    """不确定/歧义/并发运行需要显式处理，不能自动假装未执行。"""


class RecoveryFencedError(RecoveryBlockedError):
    """当前运行已失去独占权，不接受过期 worker 的产物。"""


class PlanningPreparationError(RuntimeError):
    """初始候选无法可靠产生或未通过确定性校验，未执行知识任务。"""


def _wire(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


async def _authorize(session, context):
    # 顺序不赋予权限包含关系：恢复读取和继续始终分别显式检查。
    await access._bound_context(session, context, InvestigationAction.CONTINUE)
    return await access._bound_context(session, context, InvestigationAction.READ)


@dataclass
class _ClaimGuard:
    connection: object
    backend_pid: int
    run_id: str
    generation: int

    async def check_lock(self):
        if self.connection.closed or self.connection.invalidated:
            raise RecoveryFencedError("运行锁连接已失效")
        try:
            row = (await self.connection.execute(text("SELECT pg_backend_pid(), EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted)"))).one()
        except Exception:
            raise RecoveryFencedError("无法确认本次运行的独占权") from None
        if row[0] != self.backend_pid or not row[1]:
            raise RecoveryFencedError("本次运行已失去数据库会话锁")

    async def check(self, session):
        await self.check_lock()
        current = await session.scalar(select(InvestigationRunRow).where(InvestigationRunRow.run_id == self.run_id)
                                        .with_for_update().execution_options(populate_existing=True))
        if current is None or current.current_generation != self.generation:
            raise RecoveryFencedError("运行代次已经变化，拒绝过期提交")


@dataclass(frozen=True)
class _Invocation:
    context: InvestigationRuntimeContext
    spec: FirstRoundSpec
    policy: RecoveryPolicy
    guard: _ClaimGuard
    planning_spec: PlanningSpec | None = None
    planning_claim_id: str | None = None

# 打开专用连接
#     ↓
# pg_try_advisory_lock：尝试获取运行锁
#     ↓
# 成功 → yield，执行本轮逻辑
# 失败 → 报错
#     ↓
# finally：关闭持锁连接，释放锁
@asynccontextmanager
async def _claim(engine, sessions, spec, context, policy, planning_spec=None):
    # 专用自动提交连接持会话锁；外部调用期间不持长数据库事务/行锁。
    connection = await engine.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        pid, acquired = (await connection.execute(text("SELECT pg_backend_pid(), "
            "pg_try_advisory_lock(hashtextextended(:key,0))"), {"key": f"riskmind:first-round:{spec.run_id}"})).one()
        if not acquired:
            raise RecoveryBlockedError("同一轮正在运行，拒绝并发接续")
        async with sessions.begin() as session:
            trusted = await _authorize(session, context)
            plan_row = await session.get(AcceptedPlanRow, spec.plan_id)
            if plan_row is not None and (plan_row.investigation_id, plan_row.round_id) != (spec.investigation_id, spec.round_id):
                raise ledger.LedgerIntegrityError("恢复必须从同一调查、轮次的已接受计划开始")
            if planning_spec is not None:
                await planning.register(session, planning_spec)
            if plan_row is None and planning_spec is None:
                raise ledger.LedgerIntegrityError("恢复必须从同一调查、轮次的已接受计划开始")
            generation = 0
            if plan_row is not None:
                if planning_spec is not None and await session.get(PlanningAcceptanceRow, spec.run_id) is None:
                    raise ledger.LedgerIntegrityError("规划记录缺少正式接受来源，不能跳过候选校验")
                generation = await _bind_execution_run(session, spec, plan_row, trusted, policy)
        yield _Invocation(context, spec, policy, _ClaimGuard(connection, pid, spec.run_id, generation),
                          planning_spec, "planning-claim-" + uuid4().hex)
    finally:
        # 不把持会话锁的连接放回池；连接关闭即释放全部重入锁，进程死亡也由 PG 释放。
        try:
            await asyncio.shield(connection.invalidate())
        finally:
            await connection.close()


async def _bind_execution_run(session, spec, plan_row, trusted, policy):
    """AcceptedPlan 已存在后，原有正式 Run/Claim 才能建立；调用方负责提交。"""
    plan = ledger._read(plan_row, AcceptedPlan)
    if any(task.capability != "KNOWLEDGE_RETRIEVAL" for task in plan.plan.tasks):
        raise ledger.LedgerIntegrityError("首轮恢复仅支持正式知识任务")
    row, _ = await ledger._insert(session, InvestigationRunRow, spec.run_id, spec, run_id=spec.run_id,
        investigation_id=spec.investigation_id, round_id=spec.round_id, plan_id=spec.plan_id)
    generation = (row.current_generation or 0) + 1
    claim = RunClaim(run_id=spec.run_id, generation=generation, actor_id=trusted.actor_id, policy=policy)
    session.add(InvestigationRunClaimRow(run_id=spec.run_id, generation=generation, actor_id=trusted.actor_id,
        schema_version=1, payload=canonical_json(claim), input_digest=input_digest(claim)))
    row.current_generation = generation
    await session.flush()
    return generation


def _operation(invocation, kind, *, task_id=None, candidate=None, results=()):
    spec = invocation.spec
    data = dict(investigation_id=spec.investigation_id, round_id=spec.round_id, plan_id=spec.plan_id,
                task_id=task_id, operation_type=kind)
    if kind == OperationType.QUERY_FORMULATION:
        data.update(model_name=spec.query_model, prompt_version=spec.query_prompt_digest, max_top_k=spec.max_top_k)
    elif kind == OperationType.KNOWLEDGE_EXECUTION:
        data.update(candidate_request=candidate, scope=spec.scope, index_profile=spec.index_profile)
    else:
        data.update(model_name=spec.observation_model, prompt_version=spec.observation_prompt_digest,
                    execution_refs=tuple(result.execution_id for result in results))
    return BusinessOperationInput(**data)


async def _selection(session, invocation, value, history, *, require=False):
    key = operation_key(value)
    selected = await session.get(InvestigationRunSelectionRow, (invocation.spec.run_id, key))
    committed = [fact for fact in (history or ()) if fact.artifact is not None]
    requested = invocation.policy.selected_attempts.get(key)
    if selected is not None:
        result = await session.get(ResultRow, selected.result_id)
        matches = [fact for fact in committed if result is not None and fact.attempt_id == result.attempt_id]
        if len(matches) != 1 or result.operation_key != key:
            raise ledger.LedgerIntegrityError("已固定的运行产物缺失或关联损坏")
        if requested is not None and requested != matches[0].attempt_id:
            raise ledger.LedgerConflictError("本轮已固定的历史产物不能被重新选择")
        return matches[0].artifact
    if require:
        raise ledger.LedgerIntegrityError("checkpoint 宣称完成，但账本缺少正式产物选择")
    if not committed:
        if requested is not None:
            raise RecoveryBlockedError("指定的尝试尚无已提交结果")
        return None
    matches = [fact for fact in committed if fact.attempt_id == requested] if requested else committed
    if len(matches) != 1:
        raise RecoveryBlockedError("存在多份已提交结果，需要显式选择；不自动取最新成功")
    fact = matches[0]
    row = await session.scalar(select(ResultRow).where(ResultRow.attempt_id == fact.attempt_id))
    session.add(InvestigationRunSelectionRow(run_id=invocation.spec.run_id, operation_key=key,
                                            result_id=row.result_id, generation=invocation.guard.generation))
    await session.flush()
    return fact.artifact


async def _phase(sessions, invocation, value, produce):
    """执行或复用一个阶段：先查历史，再登记尝试，最后原子提交结果。

    sessions 是数据库会话工厂；invocation 携带本次可信身份、恢复策略和运行锁检查器；
    value 是该逻辑操作的稳定业务输入；produce 是真正完成阶段工作的异步函数。
    例如 produce 可以生成 Query、检索知识或生成 Observation，本函数负责其账本边界。

    三个短事务之间不持有前一事务的行锁，但外层 _claim 的会话级运行锁仍然持有。
    sessions.begin() 正常退出时提交，异常退出时回滚；flush() 本身不代表提交。
    """
    # 事务 1：登记/核对操作，优先复用正式结果，否则结算旧运行留下的不确定尝试。
    async with sessions.begin() as session:
        # 恢复也要重新鉴权；曾经有权限，不代表当前仍有权读取和继续执行。
        await _authorize(session, invocation.context)
        # 检查持锁连接和 generation，防止失去执行权的旧运行继续修改账本。
        await invocation.guard.check(session)
        # 同一操作 key + 同一业务输入可复用；同 key 不同输入应报冲突。
        key = await access.register_authorized_operation(session, value, context=invocation.context)
        history = await access.read_operation_history(session, value, context=invocation.context)
        # 返回本轮已固定的结果，或选择唯一/显式指定的已提交结果；不擅自取“最新成功”。
        artifact = await _selection(session, invocation, value, history)
        if artifact is not None:
            # return 也会先退出事务上下文，提交结果选择，再返回；不会调用 produce。
            return artifact
        for fact in history or ():
            if fact.status == "STARTED":
                binding = await session.get(InvestigationRunAttemptRow, fact.attempt_id)
                # 只能接管属于同一个 run、且 generation 更小的旧尝试。
                # 无归属或并非旧运行的 STARTED，不能凭“没结果”就认定它已中断。
                if (binding is None or binding.run_id != invocation.spec.run_id
                        or binding.generation >= invocation.guard.generation):
                    raise RecoveryBlockedError("未结算尝试不属于已退出的旧运行，不能自动接管")
                # 只知道没有可靠保存结果，不知道外部调用是否完成，所以记 UNCERTAIN。
                await ledger.settle_attempt(session, fact.attempt_id, "UNCERTAIN", code="PREVIOUS_RUN_INTERRUPTED")
        # 结算不确定必须先可靠提交，即使默认策略阻止继续也保留真实历史。
    # 事务 2：按显式策略决定能否重试，并在调用外部服务前提交新的 STARTED。
    async with sessions.begin() as session:
        await _authorize(session, invocation.context)
        await invocation.guard.check(session)
        # 使用本事务重新读取的历史，包含事务 1 刚提交的 UNCERTAIN。
        history = await access.read_operation_history(session, value, context=invocation.context)
        if any(fact.status == "UNCERTAIN" for fact in history or ()) and not invocation.policy.retry_uncertain:
            raise RecoveryBlockedError("存在不确定尝试，当前策略不允许再次调用外部服务")
        if any(fact.status == "FAILED" for fact in history or ()) and not invocation.policy.retry_failed_attempts:
            raise RecoveryBlockedError("存在失败尝试，当前策略不允许重试")
        # 重试创建新 Attempt，不覆盖旧尝试；有历史时显式走 repository 的重试检查。
        attempt_id = "attempt-" + uuid4().hex
        await access.start_authorized_attempt(session, key, attempt_id, context=invocation.context, retry=bool(history))
        # 记录此次尝试由哪个 run / generation 发起，供下次恢复判断归属。
        session.add(InvestigationRunAttemptRow(attempt_id=attempt_id, run_id=invocation.spec.run_id,
                                              generation=invocation.guard.generation))
        await session.flush()
    # STARTED 与本次 claim 已提交；未知中断不在此伪造 FAILED，也不自动重试。
    # 外部调用放在事务之外：等待模型/检索时，不一直占用上述事务和行锁。
    # 若这里抛错或进程退出，保留 STARTED，后续恢复再判断是否结算 UNCERTAIN。
    artifact = await produce()
    # 事务 3：结果、Attempt.COMMITTED、本轮选定结果一起提交，任一失败则整体回滚。
    async with sessions.begin() as session:
        await _authorize(session, invocation.context)
        # 外部调用期间可能已经失去锁或权限，因此返回结果后也必须重新检查。
        await invocation.guard.check(session)
        # COMMITTED 表示结果可靠落库；artifact 内业务状态仍可能是 FAILED / REJECTED。
        result = await ledger.commit_result(session, attempt_id, "result-" + uuid4().hex, artifact)
        # 固定本轮采用的结果，之后恢复不能偷偷换成另一份历史产物。
        session.add(InvestigationRunSelectionRow(run_id=invocation.spec.run_id, operation_key=key,
            result_id=result.result_id, generation=invocation.guard.generation))
        await session.flush()
    # 已退出事务 3，提交成功后才把阶段产物交给调用方。
    return artifact


async def load_accepted_plan(state, invocation, *, sessions):
    """读取正式计划；新调查的候选生成与接受由 prepare_plan 先完成。"""
    async with sessions() as session:
        await _authorize(session, invocation.context)
        row = await session.get(AcceptedPlanRow, invocation.spec.plan_id)
        plan = ledger._read(row, AcceptedPlan).plan
    return _wire(dict(investigation_plan=plan, task_execution_results=[], observation=None,
                      round_complete=False, investigation_status="OPEN"))


async def prepare_plan(state, invocation, *, sessions, agent):
    """图内规划；候选和正式计划分次提交，重入时不重新生成已保存的 task_id。"""
    if invocation.guard.generation == 0:
        attempt_id, accepted = await _planning_candidate(sessions, invocation, agent)
        async with sessions.begin() as session:
            trusted = await _authorize(session, invocation.context)
            await invocation.guard.check_lock()
            facts = await planning.history(session, invocation.spec.run_id)
            fact = next((row for row in facts if row.attempt_id == attempt_id), None)
            if fact is None or fact.status != "COMMITTED":
                raise ledger.LedgerIntegrityError("正式接受必须关联可靠保存的候选计划")
            expected = planning.candidate(fact, invocation.planning_spec, invocation.context.investigation.planning_input)
            if expected != accepted:
                raise ledger.LedgerIntegrityError("候选计划在接受前发生变化")
            row = await ledger.accept_plan(session, accepted)
            session.add(PlanningAcceptanceRow(run_id=invocation.spec.run_id, attempt_id=attempt_id,
                plan_id=accepted.plan_id, investigation_id=accepted.investigation_id, round_id=accepted.round_id,
                actor_id=trusted.actor_id))
            generation = await _bind_execution_run(session, invocation.spec, row, trusted, invocation.policy)
        invocation.guard.generation = generation
    return await load_accepted_plan(state, invocation, sessions=sessions)


async def _planning_candidate(sessions, invocation, agent):
    spec, source = invocation.planning_spec, invocation.context.investigation.planning_input
    if "KNOWLEDGE_RETRIEVAL" not in source.available_capabilities:
        raise PlanningPreparationError("当前调查没有可用的知识检索能力，未调用规划模型")
    # 事务 1：优先复用已提交候选，或结算已经退出的旧 claim 留下的 STARTED。
    async with sessions.begin() as session:
        await _authorize(session, invocation.context)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.spec.run_id)
        for fact in facts:
            if fact.status == "COMMITTED":
                return fact.attempt_id, planning.candidate(fact, spec, source)
        for fact in facts:
            if fact.status == "STARTED":
                if fact.claim_id == invocation.planning_claim_id:
                    raise RecoveryBlockedError("当前规划尝试尚未结束，不能重复调用")
                await planning.settle(session, fact.attempt_id, status="UNCERTAIN", code="PREVIOUS_PLANNING_INTERRUPTED")
    # 事务 2：显式重试策略与调用前登记，不在等待模型期间持事务。
    async with sessions.begin() as session:
        trusted = await _authorize(session, invocation.context)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.spec.run_id)
        if any(row.status == "UNCERTAIN" for row in facts) and not invocation.policy.retry_uncertain:
            raise RecoveryBlockedError("初始规划存在不确定尝试，当前策略不允许重试")
        if any(row.status == "FAILED" for row in facts) and not invocation.policy.retry_failed_attempts:
            raise RecoveryBlockedError("初始规划曾失败，当前策略不允许重试")
        attempt_id = await planning.start(session, spec, context=invocation.context, trusted=trusted,
            claim_id=invocation.planning_claim_id, number=len(facts) + 1)
    try:
        async with asyncio.timeout(trusted.timeout_seconds):
            update = await asyncio.to_thread(nodes.initial_planning,
                {"planning_input": source.model_copy(deep=True)}, investigation_agent=agent)
        candidate = PlanningCandidate(plan=update["investigation_plan"])
        accepted = planning.validate(candidate, spec, source)
    except Exception as error:
        invalid = isinstance(error, (ValueError, ValidationError))
        async with sessions.begin() as session:
            await _authorize(session, invocation.context)
            await invocation.guard.check_lock()
            await planning.settle(session, attempt_id, status="FAILED" if invalid else "UNCERTAIN",
                code="INITIAL_PLANNING_INVALID" if invalid else "INITIAL_PLANNING_UNCERTAIN")
        raise PlanningPreparationError("初始规划未可靠完成或候选不合法，未接受计划、未执行知识任务") from None
    # 事务 3：合法候选和 COMMITTED 一起保存；这里还不是正式接受。
    async with sessions.begin() as session:
        await _authorize(session, invocation.context)
        await invocation.guard.check_lock()
        await planning.settle(session, attempt_id, status="COMMITTED", value=candidate)
    return attempt_id, accepted


def ledger_phase_executor(sessions, invocation):
    """把统一节点的业务产物适配到既有账本协议；首次运行和恢复不分支。"""
    async def execute(kind, produce, *, task_id=None, candidate=None, results=()):
        value = _operation(invocation, kind, task_id=task_id, candidate=candidate, results=results)
        async def persistable():
            # 每个阶段调用前重新检查可信上下文；_phase 另有 PG 归属鉴权与 fencing。
            check_runtime(invocation.context, InvestigationAction.CONTINUE)
            if kind == OperationType.QUERY_FORMULATION:
                started = datetime.now(timezone.utc)
                outcome = await produce()
                if outcome.failure is not None:
                    return PhaseArtifact(execution=outcome.failure)
                try:
                    request = KnowledgeSearchToolRequest.model_validate(
                        outcome.candidate.model_dump() if isinstance(outcome.candidate, BaseModel) else outcome.candidate)
                except (ValidationError, ValueError):
                    # 保留 8C 的入账语义：非法候选属于 Query 阶段失败，不能保存为请求。
                    error = ExecutionError(category="VALIDATION", code="QUERY_FORMULATION_INVALID",
                        message="知识查询构造结果非法，未调用检索工具", retryable=False)
                    now = datetime.now(timezone.utc)
                    return PhaseArtifact(execution=TaskExecutionResult(execution_id="exec-" + uuid4().hex,
                        task_id=task_id, capability="KNOWLEDGE_RETRIEVAL", status="FAILED",
                        started_at=started, completed_at=now, error=error))
                return PhaseArtifact(query_request=request)
            result = await produce()
            return PhaseArtifact(execution=result) if kind == OperationType.KNOWLEDGE_EXECUTION else PhaseArtifact(observation=result)
        artifact = await _phase(sessions, invocation, value, persistable)
        if kind == OperationType.QUERY_FORMULATION:
            return nodes.QueryOutcome(candidate=artifact.query_request, failure=artifact.execution)
        return artifact.execution if kind == OperationType.KNOWLEDGE_EXECUTION else artifact.observation
    return execute


class FirstRoundRecovery:
    """首轮统一持久化入口；可以图内生成计划，也可以接续已有正式计划。

    类名保留以避免无必要的改名；不要求先有计划或 checkpoint，不接受用户指定恢复模式。
    不暴露任意 thread_id、State 更新或跳步恢复。
    """

    def __init__(self, agent, *, session_factory, checkpointer, embedding_client=None, milvus_client=None):
        if not isinstance(getattr(agent, "model", None), str) or not agent.model.strip():
            raise ValueError("恢复图必须固定 Agent 模型身份")
        self._agent, self._sessions = agent, session_factory
        self._engine = session_factory.kw["bind"]
        # 运行锁不占账本池：多个长请求不能耗尽池后反过来饿死自己的短事务。
        self._lock_engine = create_async_engine(self._engine.url, poolclass=NullPool)
        from risk_platform.workflow.graph import build_investigation_graph
        self._graph = build_investigation_graph(agent, session_factory=session_factory,
            checkpointer=checkpointer, embedding_client=embedding_client, milvus_client=milvus_client)

    async def _validate(self, values, invocation):
        if not values:
            return
        spec, context = invocation.spec, invocation.context
        # v2 同时标识统一拓扑；旧 v1 不能仅因 JSON 字段相似就自动接续。
        if type(values.get("checkpoint_schema_version")) is not int or values["checkpoint_schema_version"] != 2:
            raise ledger.LedgerIntegrityError("检查点版本不兼容统一拓扑，需要显式迁移后恢复")
        if type(values.get("round_complete", False)) is not bool:
            raise ledger.LedgerIntegrityError("checkpoint 轮次结束标记必须是布尔值")
        if values.get("investigation_status", "OPEN") != "OPEN":
            raise ledger.LedgerIntegrityError("首轮恢复不能关闭案件")
        if values.get("run_id") != spec.run_id or values.get("plan_id") != spec.plan_id:
            raise ledger.LedgerIntegrityError("checkpoint 与调查运行身份不一致")
        if InitialPlanningInput.model_validate(values["planning_input"]) != context.investigation.planning_input:
            raise ledger.LedgerIntegrityError("checkpoint 的规划来源与 canonical 调查不一致")
        async with self._sessions.begin() as session:
            await _authorize(session, context)
            await invocation.guard.check_lock()
            plan_row = await session.get(AcceptedPlanRow, spec.plan_id)
            if plan_row is None:
                if invocation.planning_spec is None or "investigation_plan" in values or values.get("task_execution_results") or values.get("observation") or values.get("round_complete"):
                    raise ledger.LedgerIntegrityError("尚未接受计划的检查点不能宣称已有执行产物")
                root = await session.get(PlanningRunRow, spec.run_id)
                if root is None or ledger._read(root, PlanningSpec) != invocation.planning_spec:
                    raise ledger.LedgerIntegrityError("计划生成前的检查点缺少稳定规划记录")
                return
            await invocation.guard.check(session)
            accepted = ledger._read(plan_row, AcceptedPlan).plan
            if "investigation_plan" not in values:
                if values.get("task_execution_results") or values.get("observation") or values.get("round_complete"):
                    raise ledger.LedgerIntegrityError("checkpoint 缺正式计划却宣称已有产物")
                return
            actual_plan = InvestigationPlan.model_validate(values["investigation_plan"])
            expected_plan = accepted.model_copy(deep=True)
            results = [TaskExecutionResult.model_validate(row) for row in values.get("task_execution_results", [])]
            if len(results) not in (0, len(accepted.tasks)):
                raise ledger.LedgerIntegrityError("首轮 checkpoint 任务结果不完整")
            async def selected(value):
                history = await access.read_operation_history(session, value, context=context)
                return await _selection(session, invocation, value, history, require=True)
            for task, result in zip(expected_plan.tasks, results):
                formed = await selected(_operation(invocation, OperationType.QUERY_FORMULATION, task_id=task.task_id))
                authoritative = formed.execution
                if authoritative is None:
                    authoritative = (await selected(_operation(invocation, OperationType.KNOWLEDGE_EXECUTION,
                        task_id=task.task_id, candidate=formed.query_request))).execution
                if authoritative != result:
                    raise ledger.LedgerIntegrityError("checkpoint 执行事实与不可变账本不一致")
                task.status = TaskStatus.COMPLETED if result.status == "SUCCEEDED" else TaskStatus.FAILED
            if actual_plan != expected_plan:
                raise ledger.LedgerIntegrityError("checkpoint 计划身份、内容或任务状态不一致")
            if values.get("observation") is not None:
                if len(results) != len(accepted.tasks):
                    raise ledger.LedgerIntegrityError("解释缺少完整执行事实")
                artifact = await selected(_operation(invocation, OperationType.OBSERVATION, results=results))
                if artifact.observation != RoundObservation.model_validate(values["observation"]):
                    raise ledger.LedgerIntegrityError("checkpoint 解释与账本不一致")
            if values.get("round_complete") and values.get("observation") is None:
                raise ledger.LedgerIntegrityError("checkpoint 未解释却宣称本轮完成")
            if values.get("investigation_status", "OPEN") != "OPEN":
                raise ledger.LedgerIntegrityError("首轮恢复不能关闭案件")

    async def ainvoke(self, *, context: InvestigationInvocation, plan_id=None, policy: RecoveryPolicy | None = None):
        # 对外统一包装，内部 claim 必须由本次调用鉴权和加锁后生成。
        context = require_invocation(context).runtime
        policy = RecoveryPolicy.model_validate_json((policy or RecoveryPolicy()).model_dump_json())
        async with self._sessions() as session:
            trusted = await _authorize(session, context)  # 在任何 checkpoint 读取/反序列化前。
            case_id, round_id = context.investigation.investigation_id, context.round.round_id
            existing_run = await session.scalar(select(InvestigationRunRow).where(InvestigationRunRow.round_id == round_id))
            preparation = await session.scalar(select(PlanningRunRow).where(PlanningRunRow.round_id == round_id))
            if existing_run is not None or preparation is not None:
                owner = existing_run if existing_run is not None else preparation
                if owner.investigation_id != case_id or (plan_id is not None and plan_id != owner.plan_id):
                    raise ledger.LedgerConflictError("该轮次已经绑定其他正式计划或规划身份")
                plan_id, run_id = owner.plan_id, owner.run_id
            elif plan_id is None:
                plans = list(await session.scalars(select(AcceptedPlanRow).where(AcceptedPlanRow.round_id == round_id)))
                if len(plans) > 1:
                    raise ledger.LedgerConflictError("存在多份正式计划，必须明确提供 plan_id")
                if plans:
                    plan_id = plans[0].plan_id
                run_id = None
            else:
                run_id = None
        creating_plan = plan_id is None
        if creating_plan:
            # 启动身份只依赖案件与轮次；plan_id 是内部预留身份，不要求调用方先生成计划。
            digest = hashlib.sha256(json.dumps([case_id, round_id], separators=(",", ":")).encode()).hexdigest()
            run_id, plan_id = "round-" + digest, "plan-" + digest
        elif run_id is None:
            # 保留旧 AcceptedPlan 入口的 run_id 和历史 checkpoint 定位。
            identity = [case_id, round_id, plan_id]
            run_id = "round-" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
        spec = FirstRoundSpec(run_id=run_id, investigation_id=case_id, round_id=round_id, plan_id=plan_id,
            scope=trusted.scope, index_profile=trusted.index_profile, max_top_k=trusted.max_top_k,
            query_model=self._agent.model, observation_model=self._agent.model,
            query_prompt_digest=hashlib.sha256(KNOWLEDGE_QUERY_SYSTEM_PROMPT.encode()).hexdigest(),
            observation_prompt_digest=hashlib.sha256(KNOWLEDGE_OBSERVATION_SYSTEM_PROMPT.encode()).hexdigest())
        planning_spec = None
        if creating_plan or preparation is not None:
            planning_spec = PlanningSpec(execution=spec, planning_input_digest=input_digest(context.investigation.planning_input),
                prompt_digest=hashlib.sha256(INITIAL_PLANNING_SYSTEM_PROMPT.encode()).hexdigest())
        config = {"configurable": {"thread_id": run_id}}
        async with _claim(self._lock_engine, self._sessions, spec, context, policy, planning_spec) as invocation:
            snapshot = await self._graph.aget_state(config)
            # 旧拓扑的待执行节点不能静默跳过或映射；保留历史并明确拒绝。
            if set(snapshot.next) - {"prepare_plan", "task_execution", "observation", "replanning"}:
                raise ledger.LedgerIntegrityError("检查点包含旧拓扑节点，需要显式迁移后恢复")
            await self._validate(snapshot.values, invocation)
            if snapshot.values and not snapshot.next:
                if not snapshot.values.get("round_complete"):
                    raise ledger.LedgerIntegrityError("checkpoint 已结束但首轮事实未完成")
                output = snapshot.values
            else:
                initial = None if snapshot.values else _wire(dict(checkpoint_schema_version=2, run_id=run_id, plan_id=plan_id,
                    planning_input=context.investigation.planning_input))
                output = await self._graph.ainvoke(initial, config, context=InvestigationInvocation(runtime=context, recovery=invocation), durability="sync")
            await self._validate(output, invocation)
            return dict(run_id=run_id, plan_id=plan_id,
                planning_input=InitialPlanningInput.model_validate(output["planning_input"]),
                investigation_plan=InvestigationPlan.model_validate(output["investigation_plan"]),
                task_execution_results=[TaskExecutionResult.model_validate(row) for row in output["task_execution_results"]],
                observation=RoundObservation.model_validate(output["observation"]), round_complete=True, investigation_status="OPEN")
