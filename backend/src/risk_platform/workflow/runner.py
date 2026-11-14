"""调查持久化执行入口：鉴权、稳定运行身份、claim、调用统一图并核对检查点。"""

import hashlib
import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from risk_platform.execution.contracts import TaskExecutionResult
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan
from risk_platform.workflow.observation import RoundObservation, read_observation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.models import AcceptedPlanRow
from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, RecoveryPolicy
from risk_platform.workflow.persistence.planning_models import PlanningRunRow
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.run_guard import _claim
from risk_platform.workflow.checkpoints import to_checkpoint_data, validate_checkpoint


class InvestigationRunner:
    """首轮统一持久化入口；可以图内生成计划，也可以接续已有正式计划。

    首次执行与恢复共用此入口；不要求先有计划或 checkpoint，不接受用户指定恢复模式。
    不暴露任意 thread_id、State 更新或跳步恢复。
    """

    def __init__(self, agent, *, session_factory, checkpointer):
        if not isinstance(getattr(agent, "model", None), str) or not agent.model.strip():
            raise ValueError("恢复图必须固定 Agent 模型身份")
        self._agent, self._sessions = agent, session_factory
        self._engine = session_factory.kw["bind"]
        # 运行锁不占账本池：多个长请求不能耗尽池后反过来饿死自己的短事务。
        self._lock_engine = create_async_engine(self._engine.url, poolclass=NullPool)
        from risk_platform.workflow.graph import build_investigation_graph
        self._graph = build_investigation_graph(agent, session_factory=session_factory,
            checkpointer=checkpointer)

    async def aclose(self):
        """Release the dedicated lock engine; caller must first finish active runs."""
        await self._lock_engine.dispose()

    async def ainvoke(self, *, context: InvestigationInvocation, plan_id=None, policy: RecoveryPolicy | None = None):
        # 对外统一包装，内部 claim 必须由本次调用鉴权和加锁后生成。
        context = require_invocation(context).runtime
        policy = RecoveryPolicy.model_validate_json((policy or RecoveryPolicy()).model_dump_json())
        async with self._sessions() as session:
            trusted = await authorize_run(session, context)  # 在任何 checkpoint 读取/反序列化前。
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
        # 运行控制只确定身份；模型协议由节点阶段登记，规划日志也在图内建立。
        run_identity = RunIdentity(run_id=run_id, investigation_id=case_id, round_id=round_id, plan_id=plan_id)
        config = {"configurable": {"thread_id": run_id}}
        async with _claim(self._lock_engine, self._sessions, run_identity, context, policy,
                          allow_planning=creating_plan or preparation is not None) as invocation:
            snapshot = await self._graph.aget_state(config)
            # 旧拓扑的待执行节点不能静默跳过或映射；保留历史并明确拒绝。
            if set(snapshot.next) - {"prepare_plan", "task_execution", "observation", "replanning"}:
                raise ledger.LedgerIntegrityError("检查点包含旧拓扑节点，需要显式迁移后恢复")
            await validate_checkpoint(snapshot.values, invocation, sessions=self._sessions, runtime=context)
            if snapshot.values and not snapshot.next:
                if not snapshot.values.get("round_complete"):
                    raise ledger.LedgerIntegrityError("checkpoint 已结束但首轮事实未完成")
                output = snapshot.values
            else:
                initial = None if snapshot.values else to_checkpoint_data(dict(checkpoint_schema_version=2, run_id=run_id, plan_id=plan_id,
                    planning_input=context.investigation.planning_input))
                output = await self._graph.ainvoke(initial, config, context=InvestigationInvocation(runtime=context, execution=invocation), durability="sync")
            await validate_checkpoint(output, invocation, sessions=self._sessions, runtime=context)
            return dict(run_id=run_id, plan_id=plan_id,
                planning_input=InitialPlanningInput.model_validate(output["planning_input"]),
                investigation_plan=InvestigationPlan.model_validate(output["investigation_plan"]),
                task_execution_results=[TaskExecutionResult.model_validate(row) for row in output["task_execution_results"]],
                observation=read_observation(output["observation"]), round_complete=True, investigation_status="OPEN")
