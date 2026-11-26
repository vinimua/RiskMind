"""阶段持久化适配：构造操作输入、选择正式结果，并执行三段事务协议。"""

import asyncio
import hashlib
import json
import logging
from risk_platform.agents.prompts import TOOL_SELECTION_SYSTEM_PROMPT, OBSERVATION_SYSTEM_PROMPT
from risk_platform.memory.repository import capture_observation, context_for_stage
from risk_platform.execution.tools import query_contract_digest, available_tools, tool_definition, validate_tool_call, ToolCall
from uuid import uuid4
from pydantic import BaseModel
from sqlalchemy import select
from risk_platform.workflow import access
from risk_platform.execution.contracts import QueryOutcome
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.business_inputs import ToolOperationInput
from risk_platform.workflow.persistence.contracts import OperationType, operation_key
from risk_platform.workflow.persistence.models import ResultRow, OperationRow
from risk_platform.workflow.persistence.business_inputs import business_projection
from risk_platform.workflow.persistence.recovery_contracts import ToolArtifact, RegisteredCall
from risk_platform.workflow.persistence.recovery_models import InvestigationRunAttemptRow, InvestigationRunSelectionRow
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.run_guard import RecoveryBlockedError
from risk_platform.core.token_budget import TOKENIZER_SHA256

logger = logging.getLogger(__name__)


async def _selection(session, invocation, value, history, *, require=False):
    key = operation_key(value)
    selected = await session.get(InvestigationRunSelectionRow, (invocation.identity.run_id, key))
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
    session.add(InvestigationRunSelectionRow(run_id=invocation.identity.run_id, operation_key=key,
                                            result_id=row.result_id, generation=invocation.guard.generation))
    await session.flush()
    return fact.artifact


async def _phase(sessions, invocation, value, produce, *, runtime, recover=None, action_id=None):
    """执行或复用一个阶段：先查历史，再登记尝试，最后原子提交结果。

    sessions 是数据库会话工厂；runtime 携带可信身份；invocation 只携带恢复策略和运行锁检查器；
    value 是该逻辑操作的稳定业务输入；produce 是真正完成阶段工作的异步函数。
    例如 produce 可以生成 Query、检索知识或生成 Observation，本函数负责其账本边界。

    三个短事务之间不持有前一事务的行锁，但外层 _claim 的会话级运行锁仍然持有。
    sessions.begin() 正常退出时提交，异常退出时回滚；flush() 本身不代表提交。
    """
    # 指标历史复用前重新向数据服务验证访问；网络等待不持有数据库事务。
    if "query_model_metrics" in value.tool_limits:
        from risk_platform.execution.metrics import binding, DefinitionRequest
        async with asyncio.timeout(runtime.timeout_seconds):
            await binding(runtime).client.call("list_metric_definitions",
                DefinitionRequest(model_id=binding(runtime).model_id))
    # 事务 1：登记/核对操作，优先复用正式结果，否则结算旧运行留下的不确定尝试。
    async with sessions.begin() as session:
        # 恢复也要重新鉴权；曾经有权限，不代表当前仍有权读取和继续执行。
        await authorize_run(session, runtime)
        # 检查持锁连接和 generation，防止失去执行权的旧运行继续修改账本。
        await invocation.guard.check(session)
        legacy = await session.scalar(select(OperationRow.operation_key).where(
            OperationRow.plan_id == invocation.identity.plan_id,
            OperationRow.schema_version < 3).limit(1))
        if legacy is not None:
            # 只读历史仍由 repository 支持；续跑不得自动换协议、产生第二次外部调用。
            raise RecoveryBlockedError("Legacy stage records require explicit migration before execution")
        # 已选定结果属于历史协议。仅模型/提示词变化时可以读取旧结果，不能冒充新模型重做。
        key = operation_key(value)
        selected = await session.get(InvestigationRunSelectionRow, (invocation.identity.run_id, key))
        if selected is not None:
            stored = await session.get(OperationRow, key)
            if stored is None:
                raise ledger.LedgerIntegrityError("已选结果缺少阶段输入")
            historical = business_projection(ledger.read_operation(stored))
            fields = {"model_name", "prompt_version"}
            if historical.model_dump(exclude=fields) != value.model_dump(exclude=fields):
                raise ledger.LedgerConflictError("阶段业务输入变化，不能复用历史结果")
            history = await access.read_operation_history(session, historical, context=runtime)
            return await _selection(session, invocation, historical, history, require=True)
        # 尚未固定结果的同一操作必须保持输入一致，禁止在未知尝试后静默切换协议。
        key = await access.register_authorized_operation(session, value, context=runtime)
        history = await access.read_operation_history(session, value, context=runtime)
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
                if (binding is None or binding.run_id != invocation.identity.run_id
                        or binding.generation >= invocation.guard.generation):
                    raise RecoveryBlockedError("未结算尝试不属于已退出的旧运行，不能自动接管")
                # 只知道没有可靠保存结果，不知道外部调用是否完成，所以记 UNCERTAIN。
                await ledger.settle_attempt(session, fact.attempt_id, "UNCERTAIN", code="PREVIOUS_RUN_INTERRUPTED")
        # 结算不确定必须先可靠提交，即使默认策略阻止继续也保留真实历史。
    # 事务 2：按显式策略决定能否重试，并在调用外部服务前提交新的 STARTED。
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check(session)
        # 使用本事务重新读取的历史，包含事务 1 刚提交的 UNCERTAIN。
        history = await access.read_operation_history(session, value, context=runtime)
        if any(fact.status == "UNCERTAIN" for fact in history or ()) and not invocation.policy.retry_uncertain and recover is None:
            raise RecoveryBlockedError("存在不确定尝试，当前策略不允许再次调用外部服务")
        if any(fact.status == "FAILED" for fact in history or ()) and not invocation.policy.retry_failed_attempts:
            raise RecoveryBlockedError("存在失败尝试，当前策略不允许重试")
        # 重试创建新 Attempt，不覆盖旧尝试；有历史时显式走 repository 的重试检查。
        attempt_id = "attempt-" + uuid4().hex
        await access.start_authorized_attempt(session, key, attempt_id, context=runtime, retry=bool(history))
        # 记录此次尝试由哪个 run / generation 发起，供下次恢复判断归属。
        session.add(InvestigationRunAttemptRow(attempt_id=attempt_id, run_id=invocation.identity.run_id,
                                              generation=invocation.guard.generation))
        await session.flush()
    # STARTED 与本次 claim 已提交；未知中断不在此伪造 FAILED，也不自动重试。
    # 外部调用放在事务之外：等待模型/检索时，不一直占用上述事务和行锁。
    # 若这里抛错或进程退出，保留 STARTED，后续恢复再判断是否结算 UNCERTAIN。
    artifact = await (recover() if recover is not None and history else produce())
    # 事务 3：结果、Attempt.COMMITTED、本轮选定结果一起提交，任一失败则整体回滚。
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        # 外部调用期间可能已经失去锁或权限，因此返回结果后也必须重新检查。
        await invocation.guard.check(session)
        # COMMITTED 表示结果可靠落库；artifact 内业务状态仍可能是 FAILED / REJECTED。
        result = await ledger.commit_result(session, attempt_id, "result-" + uuid4().hex, artifact)
        if action_id is not None:
            from risk_platform.execution.actions import _audit
            from risk_platform.workflow.persistence.models import ControlledActionRow
            action = await session.get(ControlledActionRow, action_id, with_for_update=True)
            action.result_id = result.result_id
            if action.status not in {"REJECTED", "EXPIRED", "INVALIDATED"}:
                action.status = artifact.execution.status.value
            _audit(session, action, runtime.actor_id, "RESULT_COMMITTED", result_id=result.result_id)
        if artifact.observation is not None:
            # 纯确定性提取；与正式 Observation 同事务提交，恢复不重复写记忆。
            await capture_observation(session, result)
        # 固定本轮采用的结果，之后恢复不能偷偷换成另一份历史产物。
        session.add(InvestigationRunSelectionRow(run_id=invocation.identity.run_id, operation_key=key,
            result_id=result.result_id, generation=invocation.guard.generation))
        await session.flush()
    # 已退出事务 3，提交成功后才把阶段产物交给调用方。
    return artifact

async def query_phase(produce, *, sessions, call, model_name, task_id, capability, skill_materials=()):
    """生成或复用工具请求；只有实际生成时才读取阶段记忆。"""
    execution, runtime = call.execution, call.runtime
    identity = execution.identity
    definitions = available_tools(capability, runtime)
    value = ToolOperationInput(investigation_id=identity.investigation_id, round_id=identity.round_id,
        plan_id=identity.plan_id, task_id=task_id, operation_type=OperationType.QUERY_FORMULATION,
        capability=capability.value, model_name=model_name,
        prompt_version=query_contract_digest(TOOL_SELECTION_SYSTEM_PROMPT +
            json.dumps(skill_materials, ensure_ascii=False, sort_keys=True),
            capability=capability, names=tuple(d.tool.name for d in definitions)),
        tool_limits={d.tool.name: d.stage_limits(runtime) for d in definitions})
    async def generate():
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await execution.guard.check(session)
            memory = await context_for_stage(session, runtime=runtime,
                run_id=execution.identity.run_id,
                stage="QUERY:" + hashlib.sha256(value.task_id.encode()).hexdigest())
        options = {"memory_context": memory} if memory.memories else {}
        if any(d.tool.name == "query_model_metrics" for d in definitions):
            from functools import partial
            from risk_platform.execution.metric_reads import read_events, read_definition
            async with sessions.begin() as session:
                await authorize_run(session, runtime)
                await execution.guard.check(session)
                events = await read_events(session, identity.run_id, task_id)
            options.update(definition_events=events, definition_reader=partial(read_definition,
                sessions=sessions, invocation=execution, runtime=runtime, task_id=task_id))
        outcome = await produce(**options)
        if outcome.failure is not None:
            return ToolArtifact(execution=outcome.failure)
        selected = validate_tool_call(ToolCall(outcome.tool_name, outcome.candidate), value.capability, runtime)
        return ToolArtifact(query_request=RegisteredCall(tool_name=selected.tool_name,
            arguments=selected.arguments.model_dump(mode="json")))

    artifact = await _phase(sessions, execution, value, generate, runtime=runtime)
    if artifact.execution is not None:
        return QueryOutcome(failure=artifact.execution)
    return QueryOutcome(candidate=artifact.query_request.arguments,
                              tool_name=artifact.query_request.tool_name)


async def tool_phase(produce, *, sessions, call, task_id, candidate, tool_name, capability):
    """保存或复用工具结果，可靠返回后才记录完成日志。"""
    execution, runtime = call.execution, call.runtime
    identity = execution.identity
    definition = tool_definition(tool_name)
    if definition.capability != capability:
        raise ValueError("Tool capability differs from the accepted task")
    value = ToolOperationInput(investigation_id=identity.investigation_id, round_id=identity.round_id,
        plan_id=identity.plan_id, task_id=task_id, operation_type=OperationType.TOOL_EXECUTION,
        capability=capability.value, tool_name=tool_name,
        candidate_request=candidate.model_dump(mode="json") if isinstance(candidate, BaseModel) else candidate,
        tool_limits={tool_name: definition.stage_limits(runtime)})
    async def execute_tool():
        # 结果与任务、请求及协议的对应关系由账本提交边界统一校验。
        return ToolArtifact(execution=await produce())
    if definition.approval is not None:
        from risk_platform.execution.actions import approval_gate, execute_approved
        action = await approval_gate(sessions, call, value, definition)
        async def execute_controlled():
            return ToolArtifact(execution=await execute_approved(sessions, call, action, definition, produce))
        artifact = await _phase(sessions, execution, value, execute_controlled, runtime=runtime,
            recover=execute_controlled, action_id=action.action_id)
    else:
        artifact = await _phase(sessions, execution, value, execute_tool, runtime=runtime)
    result = artifact.execution
    logger.info("tool_result", extra={
        "operation_key": operation_key(value), "tool_name": value.tool_name,
        "execution_id": result.execution_id, "task_id": result.task_id,
        "execution_status": result.status.value,
        "error_code": result.error.code if result.error else None,
        "retryable": result.error.retryable if result.error else False,
    })
    return result

# ① 构造本轮解释的阶段输入
#    案件、轮次、计划、模型、Prompt 摘要、执行结果 ID
#     ↓
# ② 把 explain() 返回的 Observation 包装为 ToolArtifact
#    交给 _phase() 保存或复用
#     ↓
# ③ 从账本产物中取出 Observation，返回给节点
async def observation_phase(produce, *, sessions, call, model_name, results, skill_materials=()):
    """保存或复用 Observation；记忆仍由 _phase 在提交事务中提取。"""
    execution, runtime = call.execution, call.runtime
    identity = execution.identity
    value = ToolOperationInput(investigation_id=identity.investigation_id, round_id=identity.round_id,
        plan_id=identity.plan_id, operation_type=OperationType.OBSERVATION, model_name=model_name,
        prompt_version=hashlib.sha256((OBSERVATION_SYSTEM_PROMPT + TOKENIZER_SHA256 +
            json.dumps(skill_materials, ensure_ascii=False, sort_keys=True)).encode()).hexdigest(),
        execution_refs=tuple(r.execution_id for r in results))
    async def explain():
        return ToolArtifact(observation=await produce())

    artifact = await _phase(sessions, execution, value, explain, runtime=runtime)
    return artifact.observation
