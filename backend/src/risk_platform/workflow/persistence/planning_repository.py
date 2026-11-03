"""计划日志的短事务操作；调用方持有同一 run 的会话锁并负责提交。"""

from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from risk_platform.workflow.contracts import CapabilityType
from . import repository as ledger
from .business_inputs import AttemptAttribution
from .contracts import AcceptedPlan, canonical_json, input_digest
from .planning_contracts import PlanningCandidate, PlanningSpec
from .planning_models import PlanningAttemptRow, PlanningRunRow


async def register(session, spec: PlanningSpec):
    execution = spec.execution
    return (await ledger._insert(session, PlanningRunRow, execution.run_id, spec,
        run_id=execution.run_id, investigation_id=execution.investigation_id,
        round_id=execution.round_id, plan_id=execution.plan_id))[0]


async def history(session, run_id):
    return list(await session.scalars(select(PlanningAttemptRow).where(PlanningAttemptRow.run_id == run_id)
        .order_by(PlanningAttemptRow.attempt_number).with_for_update().execution_options(populate_existing=True)))


def candidate(row, spec, source):
    value = PlanningCandidate.model_validate_json(row.candidate)
    if row.candidate_digest != input_digest(value):
        raise ledger.LedgerIntegrityError("候选计划摘要不一致")
    return validate(value, spec, source)


def validate(value, spec, source):
    value = PlanningCandidate.model_validate_json(value.model_dump_json())
    plan = value.plan
    if not plan.goal.strip() or any(not t.objective.strip() or not t.expected_result.strip() for t in plan.tasks):
        raise ValueError("计划目标、任务目标和预期结果不能为空")
    allowed = set(source.available_capabilities) & {CapabilityType.KNOWLEDGE_RETRIEVAL}
    if any(t.capability not in allowed for t in plan.tasks):
        raise ValueError("初始计划只能使用当前调查允许的知识检索能力")
    # 只构造待接受值做校验；此处不会写入正式计划。
    return AcceptedPlan(investigation_id=spec.execution.investigation_id,
        round_id=spec.execution.round_id, plan_id=spec.execution.plan_id, plan_version=1, plan=plan)


async def start(session, spec, *, context, trusted, claim_id, number):
    attribution = AttemptAttribution(investigation_id=context.investigation.investigation_id,
        round_id=context.round.round_id, actor_id=trusted.actor_id, execution_context=trusted)
    row = PlanningAttemptRow(attempt_id="planning-attempt-" + uuid4().hex, run_id=spec.execution.run_id,
        attempt_number=number, actor_id=trusted.actor_id, claim_id=claim_id,
        attribution=canonical_json(attribution), attribution_digest=input_digest(attribution), status="STARTED")
    session.add(row)
    await session.flush()
    return row.attempt_id


async def settle(session, attempt_id, *, status, code=None, value=None):
    row = await session.scalar(select(PlanningAttemptRow).where(PlanningAttemptRow.attempt_id == attempt_id)
        .with_for_update().execution_options(populate_existing=True))
    if row is None or row.status != "STARTED":
        raise ledger.LedgerConflictError("规划尝试已经结算，不能覆盖终态")
    row.status, row.ended_at, row.resolution_code = status, max(datetime.now(timezone.utc), row.started_at), code
    if value is not None:
        row.candidate, row.candidate_digest = canonical_json(value), input_digest(value)
    await session.flush()
    return row
