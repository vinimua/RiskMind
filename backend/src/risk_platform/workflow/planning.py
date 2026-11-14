"""图内计划准备：生成候选、确定性校验、接受入账，或加载已接受计划。"""

import asyncio
import hashlib
from risk_platform.agents.prompts import INITIAL_PLANNING_SYSTEM_PROMPT
from pydantic import ValidationError
from risk_platform.memory.repository import context_for_stage
from risk_platform.workflow import nodes
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


def planning_specification(execution, runtime, agent):
    """记录“按什么输入生成候选”，供规划日志登记和恢复时比较。

    这里不调用模型，也不生成任务。planning.register() 才负责把这份记录落库、
    比较同一运行是否仍使用相同的规划输入，避免把旧问题的候选用于新问题。
    """
    return PlanningInput(
        execution=execution.identity,  # 候选将归属的案件、轮次、运行及预留计划 ID。
        planning_model=agent.model,  # 只记录初始规划模型，不固定后续阶段的模型。
        planning_input_digest=input_digest(runtime.investigation.planning_input),
        # 保存提示词指纹用于比较；这不是提示词正文，也不保证模型输出完全一致。
        prompt_digest=hashlib.sha256(INITIAL_PLANNING_SYSTEM_PROMPT.encode()).hexdigest(),
    )


async def load_accepted_plan(state, invocation, *, sessions, runtime):
    """读取正式计划；新调查的候选生成与接受由 prepare_plan 先完成。"""
    async with sessions() as session:
        await authorize_run(session, runtime)
        row = await session.get(AcceptedPlanRow, invocation.identity.plan_id)
        plan = ledger._read(row, AcceptedPlan).plan
    return to_checkpoint_data(dict(investigation_plan=plan, task_execution_results=[], observation=None,
                      round_complete=False, investigation_status="OPEN"))


async def prepare_plan(state, invocation, *, sessions, agent, runtime):
    """让后续任务节点只消费已经正式接受的计划。

    候选先独立保存：模型返回后即使进程中断，恢复也能复用原候选和 task_id。
    正式接受再单独提交：计划、候选来源和运行代次必须一起生效，不能留下半套关联。
    已经绑定正式运行代次时跳过候选生成，统一从数据库加载正式计划。
    """
    # 0 表示尚未绑定正式执行代次，不代表没有锁或没有权限；外层 Runner 已取得运行锁。
    if invocation.guard.generation == 0:
        spec = planning_specification(invocation, runtime, agent)
        # 可能调用模型，也可能复用已提交候选；返回时候选已可靠保存。
        # accepted 是“待接受的 AcceptedPlan 对象”，此时还不是正式计划表中的记录。
        attempt_id, accepted = await _planning_candidate(sessions, invocation, agent, runtime=runtime)

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


async def _planning_candidate(sessions, invocation, agent, *, runtime):
    """取得可靠候选，尚不正式接受；COMMITTED 仅表示候选已经可靠保存。"""
    spec = planning_specification(invocation, runtime, agent)
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
            update = await asyncio.to_thread(nodes.initial_planning,
                {"planning_input": source.model_copy(deep=True)}, investigation_agent=agent, memory_context=memory_context)
        candidate = PlanningCandidate(plan=update["investigation_plan"])
        accepted = planning.validate(candidate, spec, source)
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
