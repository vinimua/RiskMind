"""图内计划准备：生成候选、确定性校验、接受入账，或加载已接受计划。"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from sqlalchemy import select
from risk_platform.workflow import access
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence.contracts import RoundProgression, RoundRecord, OperationType, operation_identity_key
from risk_platform.workflow.persistence.models import RoundProgressionRow, RoundRow, ResultRow, OperationRow
from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, read_run_identity
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow, InvestigationRunSelectionRow
from risk_platform.workflow.persistence.business_inputs import business_projection
from risk_platform.workflow.run_guard import RunExecutionContext, RunGuard
from risk_platform.workflow.observation import read_observation
from risk_platform.agents.prompts import INITIAL_PLANNING_SYSTEM_PROMPT
from pydantic import ValidationError
from risk_platform.memory.repository import context_for_stage
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import AcceptedPlan, input_digest
from risk_platform.workflow.persistence.models import AcceptedPlanRow
from risk_platform.workflow.persistence.planning_contracts import PlanningCandidate, PlanningInput
from risk_platform.workflow.persistence.planning_models import PlanningAcceptanceRow
from risk_platform.workflow.persistence import planning_repository as planning
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.run_guard import RecoveryBlockedError, _bind_execution_run
from risk_platform.workflow.checkpoints import to_checkpoint_data


class PlanningPreparationError(RuntimeError):
    """初始候选无法可靠产生或未通过确定性校验，未执行知识任务。"""


def planning_specification(execution, runtime, agent, material=None):
    """记录“按什么输入生成候选”，供规划日志登记和恢复时比较。

    这里不调用模型，也不生成任务。planning.register() 才负责把这份记录落库、
    比较同一运行是否仍使用相同的规划输入，避免把旧问题的候选用于新问题。
    """
    return PlanningInput(
        execution=execution.identity,  # 候选将归属的案件、轮次、运行及预留计划 ID。
        planning_model=agent.model,  # 只记录初始规划模型，不固定后续阶段的模型。
        planning_input_digest=input_digest(runtime.investigation.planning_input),
        # 保存提示词指纹用于比较；这不是提示词正文，也不保证模型输出完全一致。
        prompt_digest=hashlib.sha256((INITIAL_PLANNING_SYSTEM_PROMPT +
            (json.dumps(to_checkpoint_data(material), ensure_ascii=False, sort_keys=True)
             if material else "")).encode()).hexdigest(),
    )


async def load_accepted_plan(state, invocation, *, sessions, runtime):
    """读取正式计划；新调查的候选生成与接受由 prepare_plan 先完成。"""
    async with sessions() as session:
        await authorize_run(session, runtime)
        row = await session.get(AcceptedPlanRow, invocation.identity.plan_id)
        plan = ledger._read(row, AcceptedPlan).plan
    return to_checkpoint_data(dict(investigation_plan=plan, task_execution_results=[], observation=None,
                      round_complete=False, investigation_status="OPEN"))

# 模型这次会看到：
# 原始调查目标
# 历史发现和未解决问题
# 已执行任务及结果
# 当前能力
# 剩余任务预算
async def prepare_plan(state, invocation, *, sessions, agent, runtime):
    """让后续任务节点只消费已经正式接受的计划。

    候选先独立保存：模型返回后即使进程中断，恢复也能复用原候选和 task_id。
    正式接受再单独提交：计划、候选来源和运行代次必须一起生效，不能留下半套关联。
    已经绑定正式运行代次时跳过候选生成，统一从数据库加载正式计划。
    """
    # 0 表示尚未绑定正式执行代次，不代表没有锁或没有权限；外层 Runner 已取得运行锁。
    if invocation.guard.generation == 0:
        async with sessions() as session:
            material = await round_material(session, invocation, runtime)
        spec = planning_specification(invocation, runtime, agent, material)
        # 可能调用模型，也可能复用已提交候选；返回时候选已可靠保存。
        # accepted 是“待接受的 AcceptedPlan 对象”，此时还不是正式计划表中的记录。
        attempt_id, accepted = await _planning_candidate(sessions, invocation, agent, runtime=runtime, material=material)

        # 接受事务不包含模型调用。正常退出提交，任一校验或写入失败则全部回滚。
        async with sessions.begin() as session:
            # 等待模型期间权限或连接可能变化，因此在正式写入边界再次检查。
            trusted = await authorize_run(session, runtime)
            await invocation.guard.check_lock()

            # 重新读取落库事实；history() 对这些尝试行加 FOR UPDATE，保护本事务内检查。
            facts = await planning.history(session, invocation.identity.run_id)
            fact = next((row for row in facts if row.attempt_id == attempt_id), None)
            # 正式计划必须追溯到本次运行中可靠保存的候选，不能只相信内存中的返回值。
            if fact is None or fact.status != "COMMITTED":
                raise ledger.LedgerIntegrityError("正式接受必须关联可靠保存的候选计划")

            # expected 来自数据库原文，重验候选摘要和业务规则；accepted 来自函数返回值。
            # 两者必须一致，防止“保存的是候选 A，实际接受的却变成 B”。
            expected = planning.candidate(fact, spec, runtime.investigation.planning_input)
            if expected != accepted:
                raise ledger.LedgerIntegrityError("候选计划在接受前发生变化")
            if material and len(accepted.plan.tasks) > material["remaining_task_budget"]:
                raise ledger.LedgerIntegrityError("下一轮计划超过累计任务预算")

            # 以下三项属于同一次原子提交；add/flush 都不能替代事务最终的 commit。
            row = await ledger.accept_plan(session, accepted)  # 1. 正式计划及任务。
            session.add(PlanningAcceptanceRow(  # 2. 候选尝试 → 正式计划，以及接受者审计。
                run_id=invocation.identity.run_id, attempt_id=attempt_id,
                plan_id=accepted.plan_id, investigation_id=accepted.investigation_id,
                round_id=accepted.round_id, actor_id=trusted.actor_id,
            ))
            # 3. 建立正式运行和本次代次，后续阶段才能通过 generation 检查。
            generation = await _bind_execution_run(session, invocation.identity, row, trusted, invocation.policy)

        # 先确保数据库提交成功，再更新内存；回滚时不能让 guard 宣称已有正式代次。
        invocation.guard.generation = generation

    # 初次接受与恢复已有计划都从 authoritative store 读取，返回给 LangGraph 更新 State。
    return await load_accepted_plan(state, invocation, sessions=sessions, runtime=runtime)


async def _planning_candidate(sessions, invocation, agent, *, runtime, material=None):
    """取得可靠候选，尚不正式接受；COMMITTED 仅表示候选已经可靠保存。"""
    spec = planning_specification(invocation, runtime, agent, material)
    source = runtime.investigation.planning_input
    claim_id = "planning-claim-" + invocation.guard.owner_id
    from risk_platform.execution.tools import TOOL_DEFINITIONS
    if not set(source.available_capabilities) & {d.capability for d in TOOL_DEFINITIONS.values()}:
        raise PlanningPreparationError("当前调查没有可用的知识检索能力，未调用规划模型")
    # 事务 1：优先复用已提交候选，或结算已经退出的旧 claim 留下的 STARTED。
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        memory_context = await context_for_stage(session, runtime=runtime,
            run_id=invocation.identity.run_id, stage="PLANNING")
        # 首次登记规划输入；已有记录则比较，输入变化时拒绝覆盖旧候选的生成依据。
        await planning.register(session, spec)
        facts = await planning.history(session, invocation.identity.run_id)
        for fact in facts:
            if fact.status == "COMMITTED":
                return fact.attempt_id, planning.candidate(fact, spec, source)
        for fact in facts:
            if fact.status == "STARTED":
                if fact.claim_id == claim_id:
                    raise RecoveryBlockedError("当前规划尝试尚未结束，不能重复调用")
                await planning.settle(session, fact.attempt_id, status="UNCERTAIN", code="PREVIOUS_PLANNING_INTERRUPTED")
    # 事务 2：显式重试策略与调用前登记，不在等待模型期间持事务。
    async with sessions.begin() as session:
        trusted = await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.identity.run_id)
        if any(row.status == "UNCERTAIN" for row in facts) and not invocation.policy.retry_uncertain:
            raise RecoveryBlockedError("初始规划存在不确定尝试，当前策略不允许重试")
        if any(row.status == "FAILED" for row in facts) and not invocation.policy.retry_failed_attempts:
            raise RecoveryBlockedError("初始规划曾失败，当前策略不允许重试")
        attempt_id = await planning.start(session, spec, context=runtime, trusted=trusted,
            claim_id=claim_id, number=len(facts) + 1)
    # STARTED 已提交后才发出模型请求；崩溃时可知道曾尝试调用，不把未知当作未执行。
    # 这里没有持续开启账本事务，外层会话级运行锁仍由 Runner 持有。
    try:
        async with asyncio.timeout(trusted.timeout_seconds):
            options = {"memory_context": memory_context} if memory_context.memories else {}
            plan = await asyncio.to_thread(agent.initial_plan, source.model_copy(deep=True),
                                           **options, **(material or {}))
        candidate = PlanningCandidate(plan=plan)
        accepted = planning.validate(candidate, spec, source)
        if len(accepted.plan.tasks) > (material or {}).get("remaining_task_budget", invocation.max_tasks):
            raise ValueError("候选计划超过累计任务预算")
    except Exception as error:
        invalid = isinstance(error, (ValueError, ValidationError))
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await invocation.guard.check_lock()
            await planning.settle(session, attempt_id, status="FAILED" if invalid else "UNCERTAIN",
                code="INITIAL_PLANNING_INVALID" if invalid else "INITIAL_PLANNING_UNCERTAIN")
        raise PlanningPreparationError("初始规划未可靠完成或候选不合法，未接受计划、未执行知识任务") from None
    # 事务 3：合法候选和 COMMITTED 一起保存；这里还不是正式接受。
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        await planning.settle(session, attempt_id, status="COMMITTED", value=candidate)
    return attempt_id, accepted

# | 参数 | 在这里的作用 |
# |---|---|
# | `state` | 流程当前的数据，包含希望使用的 `run_id`、`plan_id` |
# | `root` | Runner 最初建立的可信调用上下文，包含原始身份和已取得的运行锁 |
# | `sessions` | 创建数据库会话，用来核对正式轮次关系和运行记录 |
# 代码逻辑：
# 1. 从 State 读取目标 run_id。
# 2. 从 root 身份开始查找。
# 3. 检查权限和原运行锁。
# 4. 沿数据库正式后继关系逐轮前进。
# 5. 到达目标后，核对 plan_id。
# 6. 读取当前轮次及运行记录，构造当前上下文。
async def current_round_call(state, root, *, sessions):
    """Resolve checkpoint identity only through immutable successor links."""
    target = state.get("run_id", root.execution.identity.run_id)
    identity = root.execution.identity
    async with sessions() as session:
        await authorize_run(session, root.runtime)
        await root.execution.guard.check_lock()
# - 当前身份仍允许运行这个案件及起始轮次。
# - 本次执行持有的数据库运行锁仍然有效。
# 两者含义不同：有业务权限，不代表可以与另一个执行者同时推进；持有锁，也不代表用户有业务权限。
# 检查失败就抛异常，后面的节点不会继续执行。
        visited = set()
        # 其中 visited 保存已经走过的运行 ID，防止循环继承关系导致无限循环。
        while identity.run_id != target:
            if identity.run_id in visited:
                raise ledger.LedgerIntegrityError("Cyclic round succession")
            visited.add(identity.run_id)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            row = await session.get(RoundProgressionRow, identity.round_id)
            if row is None:
                raise ledger.LedgerIntegrityError("Checkpoint round has no accepted predecessor")
            link = ledger._read(row, RoundProgression)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            if (link.root_run_id != root.execution.identity.run_id or link.source_run_id != identity.run_id
                    or link.investigation_id != identity.investigation_id or not link.next_round_id):
                raise ledger.LedgerIntegrityError("Checkpoint successor is not part of this investigation run")
            identity = RunIdentity(run_id=link.next_run_id, investigation_id=link.investigation_id,
                round_id=link.next_round_id, plan_id=link.next_plan_id)
        if state.get("plan_id", identity.plan_id) != identity.plan_id:
            raise ledger.LedgerIntegrityError("Checkpoint plan differs from accepted successor")
        # 如果仍在第一轮，直接返回原始身份；否则返回 successor 链上找到的身份，供后续节点使用。
        if identity == root.execution.identity:
            return root
        record = ledger._read(await session.get(RoundRow, identity.round_id), RoundRecord)
        runtime = replace(root.runtime, round=record)
        await authorize_run(session, runtime)
        run = await session.get(InvestigationRunRow, identity.run_id)
        if run is not None and read_run_identity(run) != identity:
            raise ledger.LedgerIntegrityError("Successor execution identity differs from ledger")
        generation = run.current_generation if run is not None else 0
#     没有重新获取一把锁，而是继续使用原来的锁连接。
# 同时让新的 Guard 对应当前的 run_id 和代次：
    parent_guard = root.execution.guard
    guard = RunGuard(parent_guard.connection, parent_guard.backend_pid, identity.run_id, generation,
                     owner_id=parent_guard.owner_id)
    execution = RunExecutionContext(identity, root.execution.policy, guard,
        root.execution.max_rounds, root.execution.max_tasks, root.execution.max_duration_seconds)
    return InvestigationInvocation(runtime=runtime, execution=execution)

# 先确认当前轮次及其运行身份
#     ↓
# 再让 prepare_plan 使用正确身份生成并接受本轮计划


async def round_material(session, invocation, runtime):
# 从正式历史整理下一轮材料
# 以前查到了什么？做过哪些任务？还有多少任务额度？
# 它不调用模型，也不决定下一轮任务。
    observations, tasks = [], []
    links = list(await session.scalars(select(RoundProgressionRow).where(
        RoundProgressionRow.investigation_id == runtime.investigation.investigation_id,
        RoundProgressionRow.next_round_id.is_not(None)).order_by(RoundProgressionRow.created_at)))
    for row in links:
        link = ledger._read(row, RoundProgression)
        run = await session.get(InvestigationRunRow, link.source_run_id)
        identity = read_run_identity(run)
        past_runtime = replace(runtime, round=ledger._read(await session.get(RoundRow, identity.round_id), RoundRecord))
        await authorize_run(session, past_runtime)
        # Read operation history through the existing result authorization path.
        result_row = await session.get(ResultRow, link.observation_result_id)
        operation = business_projection(ledger.read_operation(await session.get(OperationRow, result_row.operation_key)))
        facts = await access.read_operation_history(session, operation, context=past_runtime)
        artifact = next((f.artifact for f in facts if f.artifact is not None and
            f.artifact.observation is not None and f.artifact.observation.observation_id == result_row.observation_id), None)
        if artifact is None:
            raise ledger.LedgerIntegrityError("Progression source observation is missing")
        observations.append(artifact.observation)
        plan = ledger._read(await session.get(AcceptedPlanRow, identity.plan_id), AcceptedPlan).plan
        #读取该轮 AcceptedPlan 和执行结果；而一轮里可能有多个任务，所以还需要内层循环：
        for task, summary in zip(plan.tasks, artifact.observation.execution_summaries, strict=True):
            executed = await session.scalar(select(ResultRow).where(ResultRow.execution_id == summary.execution_id))
            result_artifact = ledger.read_artifact(executed)
            result = result_artifact.execution
# ③ 整理任务摘要
# 主要保留：            
# 任务 ID、目标、预期信息
# 执行 ID、状态、结果类型
# 工具名
# 错误信息（有则提供）
# 实际请求和数据快照（有则提供）
            item = dict(task_id=task.task_id, objective=task.objective, expected_result=task.expected_result,
                        execution_id=result.execution_id, status=result.status.value, outcome=summary.outcome,
                        tool_name=result.tool_name or "knowledge_search")
#这些用于避免模型混淆：
# 零命中：查询成功，但没找到材料
# 失败：工具没完成工作
# 拒绝：不允许执行
            if result.error is not None:
                item["error"] = result.error.model_dump(mode="json")
            if result.payload is not None:
                payload = result.payload.model_dump(mode="json") if hasattr(result.payload, "model_dump") else result.payload
                # Keep actual request and versioned data identity, without copying all result bodies.
                if "request" in payload:
                    item["request"] = payload["request"]
                if "retrieved" in payload:
                    item["snapshot"] = payload["retrieved"]["snapshot"]
            operation = business_projection(ledger.read_operation(await session.get(OperationRow, executed.operation_key)))
            if operation.candidate_request is not None:
                item["request"] = to_checkpoint_data(operation.candidate_request)
            tasks.append(item)
    return dict(previous_observations=tuple(observations), previous_tasks=tuple(tasks),
                remaining_task_budget=invocation.max_tasks - len(tasks)) if observations else {}
# 算法上是：遍历历史轮次，再遍历每轮任务，组装模型输入摘要。 查询量也会随历史轮次和任务数增加，因为循环中有逐项数据库读取。

async def advance_round(state, call, root, *, sessions):
    """advance_round()：程序决定是否真正继续Atomically accept STOP or one successor; no model call in this transaction."""
    from risk_platform.workflow.persistence.recovery_models import InvestigationRunSelectionRow
    async with sessions.begin() as session:
        await authorize_run(session, call.runtime)
        await call.execution.guard.check(session)
        identity = call.execution.identity
        key = operation_identity_key(identity.investigation_id, identity.round_id, identity.plan_id, None, OperationType.OBSERVATION)
# InvestigationRunSelectionRow：记录“本轮选了哪个结果”
# ResultRow：保存“结果具体是什么”
# 选择表：
# run-1 + observation操作编号 → result-123
# 结果表：
# result-123 → 本轮发现、未解决问题、继续或停止建议
        selection = await session.get(InvestigationRunSelectionRow, (identity.run_id, key))
        if selection is None:
            raise ledger.LedgerIntegrityError("Cannot advance an uncommitted observation")
        result_row = await session.get(ResultRow, selection.result_id)
        observation = ledger.read_artifact(result_row).observation
        if observation != read_observation(to_checkpoint_data(state["observation"])):
            raise ledger.LedgerIntegrityError("Progression must consume the selected observation")

        # 检查当前轮次是否已经存在，如果存在，则检查是否属于当前调查链，以及是否与当前轮次的解释结果一致。
#         如果之前已保存决定，就核对根运行及来源结果，复用原决定。
# 这样即使推进记录提交后、检查点保存前中断，也不会恢复时创建另一轮。
        existing = await session.get(RoundProgressionRow, identity.round_id)
        if existing is not None:
            decision = ledger._read(existing, RoundProgression)
            if decision.root_run_id != root.execution.identity.run_id or decision.observation_result_id != result_row.result_id:
                raise ledger.LedgerIntegrityError("Round already belongs to another progression")
        else:
            material = await round_material(session, call.execution, call.runtime)
            spent = len(material.get("previous_tasks", ())) + len(state["task_execution_results"])
            number = call.runtime.round.round_number - root.runtime.round.round_number + 1
            reason = None
            if observation.status != "SUCCEEDED":
                reason = "本轮解释未能可靠完成；调查仍未解决"
            elif observation.action == "STOP":
                reason = observation.stop_reason
            elif not observation.unresolved_questions:
                reason = "没有剩余待调查问题；不代表已确认根因"
            elif number >= call.execution.max_rounds:
                reason = "调查轮次预算已耗尽；不代表根因已确认"
            elif spent >= call.execution.max_tasks:
                reason = "累计任务预算已耗尽；不代表根因已确认"
            origin = await session.get(InvestigationRunRow, root.execution.identity.run_id)
            if reason is None and (datetime.now(timezone.utc) - origin.created_at).total_seconds() >= call.execution.max_duration_seconds:
                reason = "调查时间预算已耗尽；不代表根因已确认"
            from risk_platform.execution.tools import TOOL_DEFINITIONS
            if reason is None and not any(b.name in TOOL_DEFINITIONS for b in call.runtime.tools):
                reason = "当前没有可用的已授权工具，无法继续调查"
            suffix = hashlib.sha256((root.execution.identity.run_id + identity.round_id).encode()).hexdigest()
            successor = {} if reason is not None else dict(next_round_id="round-" + suffix,
                next_run_id="run-" + suffix, next_plan_id="plan-" + suffix)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            decision = RoundProgression(root_run_id=root.execution.identity.run_id, source_run_id=identity.run_id,
                investigation_id=identity.investigation_id, source_round_id=identity.round_id,
                observation_result_id=result_row.result_id, stop_reason=reason, **successor)
            if decision.next_round_id:
                next_round = RoundRecord(investigation_id=identity.investigation_id,
                    round_id=decision.next_round_id, round_number=call.runtime.round.round_number + 1)
                await ledger.register_round(session, next_round)
                # 新轮次也必须在当前授权范围内；失败时轮次及推进记录一并回滚。
                await authorize_run(session, replace(call.runtime, round=next_round))
            await ledger._insert(session, RoundProgressionRow, identity.round_id, decision,
                source_round_id=identity.round_id, investigation_id=identity.investigation_id,
                root_run_id=decision.root_run_id, source_run_id=identity.run_id,
                observation_result_id=result_row.result_id, next_round_id=decision.next_round_id)
    if decision.next_round_id:
        return dict(run_id=decision.next_run_id, plan_id=decision.next_plan_id,
            investigation_plan=None, task_execution_results=[], observation=None,
            round_complete=False, stop_reason=None, checkpoint_schema_version=3)
    return dict(round_complete=True, stop_reason=decision.stop_reason, investigation_status="OPEN", checkpoint_schema_version=3)
