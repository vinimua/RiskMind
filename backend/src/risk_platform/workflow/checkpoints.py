"""检查点 JSON 序列化及账本一致性校验；checkpoint 不能代替权威执行账本。"""

import json
from contextlib import asynccontextmanager
from sqlalchemy import make_url
from pydantic import BaseModel
from risk_platform.execution.contracts import TaskExecutionResult
from risk_platform.workflow import access
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.observation import RoundObservation, read_observation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import AcceptedPlan, OperationType, operation_identity_key, input_digest
from risk_platform.workflow.persistence.models import AcceptedPlanRow, OperationRow
from risk_platform.workflow.persistence import planning_repository as planning
from risk_platform.workflow.persistence.business_inputs import business_projection
from risk_platform.workflow.persistence.planning_models import PlanningRunRow
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.phase_execution import _selection


class JsonCheckpointSerializer:
    def dumps(self, value):
        def error_only(obj):
            if isinstance(obj, BaseException):
                return {"workflow_error_type": type(obj).__name__}
            raise TypeError("checkpoint 只允许 JSON 数据")
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=error_only).encode("utf-8")

    def loads(self, value):
        def invalid_constant(_):
            raise ValueError("checkpoint 不允许非有限数值")
        return json.loads(value, parse_constant=invalid_constant)

    def dumps_typed(self, value):
        from langgraph.types import Interrupt
        if isinstance(value, tuple) and value and all(isinstance(item, Interrupt) for item in value):
            return "interrupt", self.dumps([{"id": item.id, "value": item.value} for item in value])
        return "json", self.dumps(value)

    def loads_typed(self, value):
        kind, data = value
        if kind == "interrupt":
            from langgraph.types import Interrupt
            return tuple(Interrupt(id=item["id"], value=item["value"]) for item in self.loads(data))
        if kind != "json":
            raise ValueError("拒绝非 JSON checkpoint")
        return self.loads(data)


@asynccontextmanager
async def postgres_checkpointer(database_url, *, setup=False):
    # 数据库初始化由部署者显式启用；不把建表藏进普通业务调用。
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError:
        raise RuntimeError("PostgreSQL 恢复需要安装 backend 的 recovery 可选依赖") from None
    url = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    async with AsyncPostgresSaver.from_conn_string(url, serde=JsonCheckpointSerializer()) as saver:
        if setup:
            await saver.setup()
        yield saver


def to_checkpoint_data(value):
    """递归转换业务模型为 JSON 数据，供检查点保存；不携带可信执行上下文。"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: to_checkpoint_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_checkpoint_data(item) for item in value]
    return value


async def validate_checkpoint(values, invocation, *, sessions, runtime):
    """恢复前及返回前对账；检查点必须与本次 claim 和账本选定结果完全一致。"""
    if not values:
        return
    identity, context = invocation.identity, runtime
    # v2 是原单轮记录；v3 结束时必须有正式推进决定。旧 v1 不自动转换。
    if type(values.get("checkpoint_schema_version")) is not int or values["checkpoint_schema_version"] not in (2, 3):
        raise ledger.LedgerIntegrityError("检查点版本不兼容统一拓扑，需要显式迁移后恢复")
    if type(values.get("round_complete", False)) is not bool:
        raise ledger.LedgerIntegrityError("checkpoint 轮次结束标记必须是布尔值")
    if values.get("investigation_status", "OPEN") != "OPEN":
        raise ledger.LedgerIntegrityError("首轮恢复不能关闭案件")
    if values.get("run_id") != identity.run_id or values.get("plan_id") != identity.plan_id:
        raise ledger.LedgerIntegrityError("checkpoint 与调查运行身份不一致")
    if InitialPlanningInput.model_validate(values["planning_input"]) != context.investigation.planning_input:
        raise ledger.LedgerIntegrityError("checkpoint 的规划来源与 canonical 调查不一致")
    async with sessions.begin() as session:
        await authorize_run(session, context)
        await invocation.guard.check_lock()
        plan_row = await session.get(AcceptedPlanRow, identity.plan_id)
        if plan_row is None:
            if values.get("investigation_plan") is not None or values.get("task_execution_results") or values.get("observation") or values.get("round_complete"):
                raise ledger.LedgerIntegrityError("尚未接受计划的检查点不能宣称已有执行产物")
            root = await session.get(PlanningRunRow, identity.run_id)
            if root is not None:
                recorded = planning.read_spec(root)
                if (recorded.execution != invocation.identity
                        or recorded.planning_input_digest != input_digest(runtime.investigation.planning_input)):
                    raise ledger.LedgerIntegrityError("规划检查点与稳定规划记录不一致")
            # 首次图调用可在 prepare_plan 进入前保存 bootstrap；此时尚无规划日志。
            return
        await invocation.guard.check(session)
        accepted = ledger._read(plan_row, AcceptedPlan).plan
        if values.get("investigation_plan") is None:
            if values.get("task_execution_results") or values.get("observation") or values.get("round_complete"):
                raise ledger.LedgerIntegrityError("checkpoint 缺正式计划却宣称已有产物")
            return
        actual_plan = InvestigationPlan.model_validate(values["investigation_plan"])
        expected_plan = accepted.model_copy(deep=True)
        results = [TaskExecutionResult.model_validate(row) for row in values.get("task_execution_results", [])]
        if len(results) not in (0, len(accepted.tasks)):
            raise ledger.LedgerIntegrityError("首轮 checkpoint 任务结果不完整")
        async def selected(kind, *, task_id=None, candidate=None):
            key = operation_identity_key(identity.investigation_id, identity.round_id, identity.plan_id, task_id, kind)
            row = await session.get(OperationRow, key)
            if row is None:
                raise ledger.LedgerIntegrityError("checkpoint 缺少对应阶段输入")
            value = business_projection(ledger.read_operation(row))
            if (value.investigation_id, value.round_id, value.plan_id, value.task_id, value.operation_type) != (
                    identity.investigation_id, identity.round_id, identity.plan_id, task_id, kind):
                raise ledger.LedgerIntegrityError("阶段记录身份与 checkpoint 不一致")
            if kind in (OperationType.KNOWLEDGE_EXECUTION, OperationType.TOOL_EXECUTION) and value.candidate_request != candidate:
                raise ledger.LedgerIntegrityError("历史工具执行与已选查询不一致")
            if kind == OperationType.OBSERVATION and value.execution_refs != tuple(r.execution_id for r in results):
                raise ledger.LedgerIntegrityError("历史解释与执行引用不一致")
            history = await access.read_operation_history(session, value, context=context)
            return await _selection(session, invocation, value, history, require=True)
        for task, result in zip(expected_plan.tasks, results):
            formed = await selected(OperationType.QUERY_FORMULATION, task_id=task.task_id)
            authoritative = formed.execution
            if authoritative is None:
                from risk_platform.workflow.persistence.recovery_contracts import RegisteredCall
                call = formed.query_request
                kind = OperationType.TOOL_EXECUTION if result.tool_name is not None else OperationType.KNOWLEDGE_EXECUTION
                authoritative = (await selected(kind, task_id=task.task_id,
                    candidate=call.arguments if isinstance(call, RegisteredCall) else
                        call.model_dump(mode="json") if kind == OperationType.TOOL_EXECUTION else call)).execution
            if authoritative != result:
                raise ledger.LedgerIntegrityError("checkpoint 执行事实与不可变账本不一致")
            task.status = TaskStatus.COMPLETED if result.status == "SUCCEEDED" else TaskStatus.FAILED
        if actual_plan != expected_plan:
            raise ledger.LedgerIntegrityError("checkpoint 计划身份、内容或任务状态不一致")
        if values.get("observation") is not None:
            if len(results) != len(accepted.tasks):
                raise ledger.LedgerIntegrityError("解释缺少完整执行事实")
            artifact = await selected(OperationType.OBSERVATION)
            if artifact.observation != read_observation(values["observation"]):
                raise ledger.LedgerIntegrityError("checkpoint 解释与账本不一致")
        if values.get("round_complete") and values.get("observation") is None:
            raise ledger.LedgerIntegrityError("checkpoint 未解释却宣称本轮完成")
        if values.get("round_complete") and values["checkpoint_schema_version"] == 3:
            from risk_platform.workflow.persistence.contracts import RoundProgression
            from risk_platform.workflow.persistence.models import RoundProgressionRow
            row = await session.get(RoundProgressionRow, identity.round_id)
            if row is None:
                raise ledger.LedgerIntegrityError("检查点停止标记缺少正式推进决定")
            decision = ledger._read(row, RoundProgression)
            if decision.next_round_id is not None or values.get("stop_reason") != decision.stop_reason:
                raise ledger.LedgerIntegrityError("检查点停止结果与推进账本不一致")
        if values.get("investigation_status", "OPEN") != "OPEN":
            raise ledger.LedgerIntegrityError("首轮恢复不能关闭案件")
