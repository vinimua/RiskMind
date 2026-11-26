"""受控动作的批准事实与图中执行边界；阶段结果仍由原账本提交。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Literal
from uuid import uuid4

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from risk_platform.execution.contracts import ExecutionError, TaskExecutionResult
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.models import ControlledActionRow, ActionAuditRow, ResultRow
from risk_platform.workflow.runtime import InvestigationResource, InvestigationAccessError


@dataclass(frozen=True)
class ApprovalPolicy:
    """具体操作提供业务语义；公共流程不按工具名称分派。"""
    ttl_seconds: int
    material_model: type[BaseModel]
    prepare: Callable[..., Awaitable[BaseModel]]
    # 展示/恢复材料时检查来源读取权限，不判断是否允许执行副作用。
    authorize_material: Callable[..., Awaitable[bool]]
    # 批准后检查当前业务条件，例如目标版本是否仍符合批准材料。
    precondition: Callable[..., Awaitable[bool]]
    verify: Callable[..., Awaitable[BaseModel | Literal["NOT_APPLIED", "UNKNOWN"]]]
    version: str = "1"

    def __post_init__(self):
        if type(self.ttl_seconds) is not int or self.ttl_seconds <= 0:
            raise ValueError("Approval lifetime must be positive")
        if not issubclass(self.material_model, BaseModel) or not self.version:
            raise TypeError("Approval material and version are required")


class ActionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: bool = Field(strict=True)


class ManualResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["SUCCEEDED", "FAILED", "NOT_APPLIED"]
    reason: str = Field(min_length=1, max_length=2000)
    evidence: str = Field(min_length=1, max_length=4000)
    result: TaskExecutionResult | None = None


def _body(row):
    if hashlib.sha256(row.payload.encode()).hexdigest() != row.input_digest:
        raise ledger.LedgerIntegrityError("Action material digest differs")
    body = json.loads(row.payload)
    if any(body[key] != getattr(row, key) for key in ("action_id", "run_id", "plan_id", "task_id")):
        raise ledger.LedgerIntegrityError("Action identity differs")
    return body


def _authorize(actor, permission, body, authorize):
    resource = InvestigationResource(body["investigation_id"], body["round_id"], body["model_id"])
    try:
        allowed = authorize(actor, permission, resource)
    except Exception:
        allowed = False
    if allowed is not True:
        raise InvestigationAccessError("Action permission denied")


def _audit(session, row, actor, event, **detail):
    session.add(ActionAuditRow(event_id="event-" + uuid4().hex, action_id=row.action_id,
        actor_id=actor, event=event, detail=json.dumps(detail, ensure_ascii=False, sort_keys=True)))


def _view(row):
    return {**_body(row), "digest": row.input_digest, "status": row.status,
        "approved": row.approved, "decided_by": row.decided_by,
        "result_id": row.result_id}


async def inspect(sessions, action_id, *, runtime):
    actor, authorize = runtime.actor_id, runtime.authorize_resource
    async with sessions.begin() as session:
        row = await session.get(ControlledActionRow, action_id, with_for_update=True)
        if row is None:
            from risk_platform.conversations.repository import ChatNotFoundError
            raise ChatNotFoundError("Action not found")
        _authorize(actor, "action:read", _body(row), authorize)
        if row.status == "PENDING_CONFIRMATION" and datetime.now(timezone.utc) >= datetime.fromisoformat(_body(row)["expires_at"]):
            row.status = "EXPIRED"
            _audit(session, row, actor, "EXPIRED")
        view = _view(row)
        events = list(await session.scalars(select(ActionAuditRow).where(ActionAuditRow.action_id == action_id)
            .order_by(ActionAuditRow.created_at, ActionAuditRow.event_id)))
        view["audit"] = [{"actor_id": e.actor_id, "event": e.event,
            "detail": json.loads(e.detail), "created_at": e.created_at.isoformat()} for e in events]
        if row.result_id:
            view["result"] = ledger.read_artifact(await session.get(ResultRow, row.result_id)).execution.model_dump(mode="json")
        from risk_platform.execution.tools import tool_definition
        body = _body(row)
        policy = tool_definition(body["tool_name"]).approval
        if policy is None or policy.version != body["contract_version"]:
            raise ledger.LedgerConflictError("Approval material protocol changed")
        material = policy.material_model.model_validate(body["material"])
    if await policy.authorize_material(sessions, runtime, material) is not True:
        raise InvestigationAccessError("Action material is no longer accessible")
    return view


async def decide(sessions, action_id, *, actor, authorize, digest, approved):
    decision = ActionDecision(digest=digest, approved=approved)
    async with sessions.begin() as session:
        row = await session.get(ControlledActionRow, action_id, with_for_update=True)
        if row is None:
            from risk_platform.conversations.repository import ChatNotFoundError
            raise ChatNotFoundError("Action not found")
        body = _body(row)
        _authorize(actor, "action:approve", body, authorize)
        if decision.digest != row.input_digest:
            raise ledger.LedgerConflictError("Approval does not match saved material")
        if row.approved is not None:
            if row.approved != approved or row.decided_by != actor:
                raise ledger.LedgerConflictError("Approval decision cannot be replaced")
            return _view(row)
        if row.status != "PENDING_CONFIRMATION":
            raise ledger.LedgerConflictError("Action cannot be decided in this state")
        now = datetime.now(timezone.utc)
        if now >= datetime.fromisoformat(body["expires_at"]):
            row.status = "EXPIRED"
            _audit(session, row, actor, "EXPIRED")
        else:
            row.approved, row.decided_by, row.decided_at = approved, actor, now
            row.status = "APPROVED" if approved else "REJECTED"
            _audit(session, row, actor, row.status, digest=digest)
        return _view(row)


async def resolve(sessions, action_id, *, actor, authorize, resolution: ManualResolution):
    """只保存有依据的人工核实，不在 HTTP 接口执行工具。"""
    async with sessions.begin() as session:
        row = await session.get(ControlledActionRow, action_id, with_for_update=True)
        if row is None:
            from risk_platform.conversations.repository import ChatNotFoundError
            raise ChatNotFoundError("Action not found")
        body = _body(row)
        _authorize(actor, "action:resolve", body, authorize)
        if resolution.digest != row.input_digest or row.status != "UNCERTAIN":
            raise ledger.LedgerConflictError("Only the saved uncertain action can be resolved")
        if not resolution.reason.strip() or not resolution.evidence.strip():
            raise ValueError("Manual resolution requires reason and evidence")
        if (resolution.outcome == "SUCCEEDED") != (resolution.result is not None):
            raise ValueError("Successful resolution requires a complete tool result")
        if resolution.result is not None and (resolution.result.task_id != row.task_id
                or resolution.result.tool_name != body["tool_name"] or resolution.result.status != "SUCCEEDED"):
            raise ValueError("Resolution belongs to another operation")
        encoded = resolution.model_dump_json()
        if row.resolution is not None and row.resolution != encoded:
            raise ledger.LedgerConflictError("Manual resolution is immutable")
        if row.resolution is None:
            row.resolution = encoded
            _audit(session, row, actor, "MANUAL_RESOLUTION", outcome=resolution.outcome,
                reason=resolution.reason, evidence=resolution.evidence)
        return _view(row)


async def approval_gate(sessions, call, value, definition):
    """先保存草稿再中断；重放只读同一份材料，不重新准备。"""
    identity, runtime, policy = call.execution.identity, call.runtime, definition.approval
    key = hashlib.sha256(f"{identity.run_id}:{value.task_id}".encode()).hexdigest()
    action_id = "action-" + key
    async with sessions() as session:
        row = await session.get(ControlledActionRow, action_id)
    if row is None:
        source = runtime.investigation.planning_input
        body = dict(action_id=action_id, run_id=identity.run_id, plan_id=identity.plan_id,
            task_id=value.task_id, investigation_id=identity.investigation_id, round_id=identity.round_id,
            model_id=source.monitoring_alert.model_id if source.monitoring_alert else source.user_query.model_id,
            tool_name=value.tool_name, contract_version=policy.version, requested_by=runtime.actor_id,
            request=value.candidate_request, limits=value.tool_limits[value.tool_name])
        _authorize(runtime.actor_id, "action:request", body, runtime.authorize_resource)
        request = definition.tool.args_schema.model_validate(value.candidate_request)
        material = await policy.prepare(sessions, runtime, request)
        body["material"] = policy.material_model.model_validate(material).model_dump(mode="json")
        body["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=policy.ttl_seconds)).isoformat()
        payload = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await call.execution.guard.check(session)
            await ledger._lock(session, "controlled-action:" + action_id)
            row = await session.get(ControlledActionRow, action_id)
            if row is None:
                # 拒绝同一案件/工具后，模型不能换参数自动绕过拒绝。
                prior = list(await session.scalars(select(ControlledActionRow).where(ControlledActionRow.status == "REJECTED")))
                blocked = any(_body(p)["investigation_id"] == identity.investigation_id
                    and _body(p)["tool_name"] == value.tool_name for p in prior)
                row = ControlledActionRow(action_id=action_id, run_id=identity.run_id,
                    plan_id=identity.plan_id, task_id=value.task_id, schema_version=1,
                    payload=payload, input_digest=hashlib.sha256(payload.encode()).hexdigest(),
                    status="REJECTED" if blocked else "PENDING_CONFIRMATION")
                session.add(row)
                await session.flush()
                _audit(session, row, runtime.actor_id, "BLOCKED_BY_REJECTION" if blocked else "CREATED")
    body = _body(row)
    if (body["tool_name"] != value.tool_name or body["contract_version"] != policy.version
            or body["request"] != value.candidate_request or body["limits"] != value.tool_limits[value.tool_name]):
        raise ledger.LedgerConflictError("Saved approval contract differs from the execution")
    _authorize(runtime.actor_id, "action:read", body, runtime.authorize_resource)
    if await policy.authorize_material(sessions, runtime, policy.material_model.model_validate(body["material"])) is not True:
        raise InvestigationAccessError("Action material is no longer accessible")
    if row.status == "REJECTED" and row.approved is None:
        return row  # 自动拦截重提，不制造第二次人工确认。
    # 同一节点重放时保持 interrupt 调用顺序稳定；恢复输入不代表批准。
    interrupt({"status": "WAITING_APPROVAL", "pending_action": _view(row)})
    async with sessions() as session:
        row = await session.get(ControlledActionRow, action_id)
        if row.status == "PENDING_CONFIRMATION":
            raise ledger.LedgerConflictError("Graph cannot resume without a saved decision")
    return row


def rejected_result(body, definition, code, *, status="REJECTED"):
    now = datetime.now(timezone.utc)
    return TaskExecutionResult(execution_id="exec-" + body["action_id"], task_id=body["task_id"],
        capability=definition.capability, tool_name=body["tool_name"], result_version=definition.result_version, status=status,
        started_at=now, completed_at=now,
        error=ExecutionError(category="AUTHORIZATION" if status == "REJECTED" else "DEPENDENCY",
            code=code, message="动作未执行或已通过核实确认失败", retryable=False))


async def execute_approved(sessions, call, row, definition, produce):
    """已有执行尝试必须先核实；只有明确未生效才允许重新执行。"""
    runtime, policy = call.runtime, definition.approval
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await call.execution.guard.check(session)
        row = await session.get(ControlledActionRow, row.action_id, with_for_update=True)
        body = _body(row)
        material = policy.material_model.model_validate(body["material"])
        recovering = row.status in {"EXECUTING", "UNCERTAIN"}
        resolution = ManualResolution.model_validate_json(row.resolution) if row.resolution else None
    if recovering:
        outcome = await policy.verify(runtime, material, row.action_id)
        source = "MANUAL" if outcome == "UNKNOWN" and resolution else "TOOL"
        if outcome == "UNKNOWN" and resolution:
            outcome = definition.result_model.model_validate(resolution.result.payload) if resolution.outcome == "SUCCEEDED" else resolution.outcome
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await call.execution.guard.check(session)
            saved = await session.get(ControlledActionRow, row.action_id, with_for_update=True)
            _audit(session, saved, runtime.actor_id, "VERIFIED", source=source,
                outcome="APPLIED" if isinstance(outcome, BaseModel) else outcome,
                receipt_digest=hashlib.sha256(outcome.model_dump_json().encode()).hexdigest() if isinstance(outcome, BaseModel) else None)
        if isinstance(outcome, BaseModel):
            payload = definition.result_model.model_validate(outcome).model_dump(mode="json")
            definition.validate_result(definition.tool.args_schema.model_validate(body["request"]), body["limits"], outcome)
            return TaskExecutionResult(execution_id="exec-" + row.action_id, task_id=row.task_id,
                capability=definition.capability, tool_name=body["tool_name"], result_version=definition.result_version,
                status="SUCCEEDED", started_at=row.created_at, completed_at=datetime.now(timezone.utc), payload=payload)
        if outcome == "FAILED":
            return rejected_result(body, definition, "MANUALLY_CONFIRMED_FAILURE", status="FAILED")
        if outcome != "NOT_APPLIED":
            async with sessions.begin() as session:
                await authorize_run(session, runtime)
                await call.execution.guard.check(session)
                saved = await session.get(ControlledActionRow, row.action_id, with_for_update=True)
                saved.status = "UNCERTAIN"
                _audit(session, saved, runtime.actor_id, "UNCERTAIN")
            interrupt({"status": "WAITING_VERIFICATION", "pending_action": _view(saved)})
            return await execute_approved(sessions, call, saved, definition, produce)
    code = None
    if row.status in {"REJECTED", "EXPIRED", "INVALIDATED"}:
        code = "ACTION_" + row.status
    elif row.approved is not True:
        raise ledger.LedgerIntegrityError("Action execution has no approval")
    elif datetime.now(timezone.utc) >= datetime.fromisoformat(body["expires_at"]):
        code = "ACTION_EXPIRED"
    else:
        _authorize(row.decided_by, "action:approve", body, runtime.authorize_resource)
        if await policy.precondition(sessions, runtime, material) is not True:
            code = "ACTION_INVALIDATED"
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await call.execution.guard.check(session)
        saved = await session.get(ControlledActionRow, row.action_id, with_for_update=True)
        if code is None:
            # precondition 可能等待外部服务；领取前重新核对批准权限和期限。
            _authorize(saved.decided_by, "action:approve", body, runtime.authorize_resource)
            if datetime.now(timezone.utc) >= datetime.fromisoformat(body["expires_at"]):
                code = "ACTION_EXPIRED"
        if code:
            saved.status = code.removeprefix("ACTION_")
            _audit(session, saved, runtime.actor_id, saved.status)
        else:
            saved.status = "EXECUTING"
            _audit(session, saved, runtime.actor_id, "EXECUTING", generation=call.execution.guard.generation)
    if code:
        return rejected_result(body, definition, code)
    try:
        return await produce(action_id=row.action_id, approved_material=material)
    except Exception:
        # 超时/客户端异常不能证明外部没有生效；BaseException 留给进程中断恢复。
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await call.execution.guard.check(session)
            saved = await session.get(ControlledActionRow, row.action_id, with_for_update=True)
            saved.status = "UNCERTAIN"
            _audit(session, saved, runtime.actor_id, "UNCERTAIN")
        interrupt({"status": "WAITING_VERIFICATION", "pending_action": _view(saved)})
        return await execute_approved(sessions, call, saved, definition, produce)
