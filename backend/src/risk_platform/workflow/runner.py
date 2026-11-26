"""调查持久化执行入口：鉴权、稳定运行身份、claim、调用统一图并核对检查点。"""

import hashlib
import json
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from langgraph.types import Command
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
    """调查统一持久化入口；跨轮保持同一检查点定位和案件运行权。

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
            # 每轮最多八次读取，主图需容纳读取回边及任务、解释节点。
            config["recursion_limit"] = invocation.max_rounds * 24 + 10
            snapshot = await self._graph.aget_state(config)
            # 旧拓扑的待执行节点不能静默跳过或映射；保留历史并明确拒绝。
            if set(snapshot.next) - {"prepare_plan", "read_skill", "task_execution", "observation"}:
                raise ledger.LedgerIntegrityError("检查点包含旧拓扑节点，需要显式迁移后恢复")
            from risk_platform.workflow.planning import current_round_call
            root = InvestigationInvocation(runtime=context, execution=invocation)
            active = await current_round_call(snapshot.values, root, sessions=self._sessions)
            await validate_checkpoint(snapshot.values, active.execution, sessions=self._sessions, runtime=active.runtime)
            pauses = [pause for task in snapshot.tasks for pause in task.interrupts]
            if pauses and pauses[0].value.get("status") == "WAITING_APPROVAL":
                from risk_platform.execution.actions import inspect as inspect_action
                pending = await inspect_action(self._sessions, pauses[0].value["pending_action"]["action_id"],
                    runtime=active.runtime)
                if pending["status"] == "PENDING_CONFIRMATION":
                    return {"run_id": run_id, "investigation_id": case_id, "round_complete": False,
                        "status": "WAITING_APPROVAL", "pending_action": pending}
            if snapshot.values and not snapshot.next and not pauses:
                if not snapshot.values.get("round_complete"):
                    raise ledger.LedgerIntegrityError("checkpoint 已结束但调查推进尚未完成")
                output = snapshot.values
            else:
                interrupted = any(task.interrupts for task in snapshot.tasks)
                initial = Command(resume=True) if interrupted else None if snapshot.values else to_checkpoint_data(dict(checkpoint_schema_version=3, run_id=run_id, plan_id=plan_id,
                    planning_input=context.investigation.planning_input))
                output = await self._graph.ainvoke(initial, config, context=InvestigationInvocation(runtime=context, execution=invocation), durability="sync")
            if output.get("__interrupt__"):
                pending = output["__interrupt__"][0].value
                if pending.get("status") not in {"WAITING_APPROVAL", "WAITING_VERIFICATION"}:
                    raise ledger.LedgerIntegrityError("Unknown graph interrupt")
                # Graph durability=sync 已保存中断；退出 _claim 才向调用者返回，释放运行锁。
                return {"run_id": run_id, "investigation_id": case_id,
                    "round_complete": False, **pending}
            active = await current_round_call(output, root, sessions=self._sessions)
            await validate_checkpoint(output, active.execution, sessions=self._sessions, runtime=active.runtime)
            # 已完成检查点也会直接返回历史指标；返回前重新验证远端访问权限。
            if any(row.get("tool_name") == "query_model_metrics" and row.get("payload") is not None
                   for row in output["task_execution_results"]):
                from risk_platform.execution.metrics import binding, DefinitionRequest
                import asyncio
                async with asyncio.timeout(active.runtime.timeout_seconds):
                    await binding(active.runtime).client.call("list_metric_definitions",
                        DefinitionRequest(model_id=binding(active.runtime).model_id))
            return dict(run_id=active.execution.identity.run_id, plan_id=output["plan_id"],
                planning_input=InitialPlanningInput.model_validate(output["planning_input"]),
                investigation_plan=InvestigationPlan.model_validate(output["investigation_plan"]),
                task_execution_results=[TaskExecutionResult.model_validate(row) for row in output["task_execution_results"]],
                observation=read_observation(output["observation"]), round_complete=True, investigation_status="OPEN",
                **({"stop_reason": output["stop_reason"]} if output.get("stop_reason") else {}),
                **({"root_run_id": run_id} if active.execution.identity.run_id != run_id else {}))
# [`current_round_call()` (line 181)]：沿数据库中的正式后继关系，确定检查点目前处于哪一轮，不能只相信 State 写的运行 ID。
# [`validate_checkpoint()` (line 69)]：核对正式计划、执行结果、Observation 等是否与账本一致。

# 校验调用上下文与恢复策略
#     ↓
# 鉴权，查找已有规划／运行身份
#     ↓
# 取得运行锁，建立本次执行控制
#     ↓
# 读取并核验检查点
#     ├─ 已结束：复用结果
#     ├─ 未开始：提交初始 State，运行图
#     └─ 已中断：从检查点继续
#     ↓
# 再次与账本对账，返回结果
#     ↓
# 释放运行锁
