"""短事务账本操作。调用方负责 begin/commit；此处从不调用外部 API。"""

from datetime import datetime, timezone

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from risk_platform.workflow.contracts import CapabilityType, TaskStatus
from risk_platform.workflow.observation import (
    KnowledgeObservationDraft, summarize_executions, validate_observation_draft, validate_round_inputs,
)
from .contracts import (
    AcceptedPlan, AttemptStatus, CommittedArtifact, Identity, InvestigationRecord, OperationInput,
    OperationType, RoundRecord, canonical_json, input_digest, operation_key,
)
from .models import AcceptedPlanRow, AttemptRow, InvestigationRow, OperationRow, PlanTaskRow, ResultRow, RoundRow
from .business_inputs import AttemptAttribution, BusinessOperationInput, business_projection


class LedgerConflictError(ValueError):
    """同一身份的内容冲突，或尝试状态不允许当前操作。"""


class LedgerIntegrityError(ValueError):
    """关联记录缺失或账本内容损坏。"""


async def _lock(session: AsyncSession, key: str) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                          {"key": f"riskmind:execution-ledger:{key}"})


def _fresh(value: BaseModel):
    # frozen 不会递归冻结旧契约内的列表；每次写入前重新验证独立副本。
    return type(value).model_validate_json(value.model_dump_json())


def _read(row, contract):
    value = contract.model_validate_json(row.payload)
    if row.schema_version != value.schema_version or row.input_digest != input_digest(value):
        raise LedgerIntegrityError("账本载荷版本或摘要不一致")
    return value


def read_operation(row) -> OperationInput | BusinessOperationInput:
    if row.schema_version not in (1, 2):
        raise LedgerIntegrityError("未知操作契约版本")
    return _read(row, OperationInput if row.schema_version == 1 else BusinessOperationInput)


async def _insert(session, row_type, identity, value, **columns):
    value = _fresh(value)
    await _lock(session, f"record:{row_type.__tablename__}:{identity}")
    existing = await session.get(row_type, identity)
    if existing is not None:
        if existing.payload != canonical_json(value) or existing.input_digest != input_digest(value):
            raise LedgerConflictError("同一记录身份对应不同输入")
        return existing, False
    row = row_type(payload=canonical_json(value), input_digest=input_digest(value), schema_version=value.schema_version, **columns)
    session.add(row)
    await session.flush()
    return row, True


async def register_investigation(session: AsyncSession, value: InvestigationRecord) -> InvestigationRow:
    return (await _insert(session, InvestigationRow, value.investigation_id, value,
                          investigation_id=value.investigation_id))[0]


async def register_round(session: AsyncSession, value: RoundRecord) -> RoundRow:
    return (await _insert(session, RoundRow, value.round_id, value, round_id=value.round_id,
                          investigation_id=value.investigation_id, round_number=value.round_number))[0]


async def accept_plan(session: AsyncSession, value: AcceptedPlan) -> AcceptedPlanRow:
    value = _fresh(value)
    row, created = await _insert(session, AcceptedPlanRow, value.plan_id, value, plan_id=value.plan_id,
                                investigation_id=value.investigation_id, round_id=value.round_id, plan_version=value.plan_version)
    if created:
        for ordinal, task in enumerate(value.plan.tasks):
            session.add(PlanTaskRow(plan_id=value.plan_id, task_id=task.task_id, ordinal=ordinal, capability=task.capability.value))
        await session.flush()
    return row


async def register_operation(session: AsyncSession, value: OperationInput | BusinessOperationInput,
                             *, created_by: str | None = None) -> OperationRow:
    value = _fresh(value)
    plan = await session.get(AcceptedPlanRow, value.plan_id)
    if plan is None or (plan.investigation_id, plan.round_id) != (value.investigation_id, value.round_id):
        raise LedgerIntegrityError("操作必须关联同一调查、轮次下的已接受计划")
    _read(plan, AcceptedPlan)
    if value.task_id is not None:
        task = await session.get(PlanTaskRow, (value.plan_id, value.task_id))
        if task is None or task.capability != CapabilityType.KNOWLEDGE_RETRIEVAL.value:
            raise LedgerIntegrityError("当前账本操作只支持计划中的正式知识任务")
    if value.operation_type == OperationType.OBSERVATION:
        await _observation_results(session, value)
    key = operation_key(value)
    if isinstance(value, BusinessOperationInput):
        TypeAdapter(Identity).validate_python(created_by)
        # 同 key 已有 v1 时保持原文和摘要，显式验证稳定业务输入，不伪装成 v2。
        await _lock(session, f"record:{OperationRow.__tablename__}:{key}")
        existing = await session.get(OperationRow, key)
        if existing is not None:
            if business_projection(read_operation(existing)) != value:
                raise LedgerConflictError("同一操作的稳定业务输入冲突")
            return existing
    return (await _insert(session, OperationRow, key, value, operation_key=key,
                          investigation_id=value.investigation_id, round_id=value.round_id,
                          plan_id=value.plan_id, task_id=value.task_id, operation_type=value.operation_type.value,
                          created_by=created_by))[0]


async def start_attempt(session: AsyncSession, key: str, attempt_id: str, *, retry: bool = False,
                        attribution: AttemptAttribution) -> AttemptRow:
    """必须在外部调用前提交本事务。retry=True 只是显式登记，不执行重试。"""
    TypeAdapter(Identity).validate_python(attempt_id)
    attribution = _fresh(attribution)
    await _lock(session, f"operation:{key}")
    operation_row = await session.get(OperationRow, key)
    if operation_row is None:
        raise LedgerIntegrityError("逻辑操作尚未登记")
    if (attribution.investigation_id, attribution.round_id) != (operation_row.investigation_id, operation_row.round_id):
        raise LedgerIntegrityError("尝试审计身份与操作归属不一致")
    # 全局 attempt 身份也必须串行化，避免同一 ID 被两个不同 operation 争抢。
    await _lock(session, f"attempt:{attempt_id}")
    existing = await session.get(AttemptRow, attempt_id, populate_existing=True)
    if existing is not None:
        if existing.operation_key != key:
            raise LedgerConflictError("尝试身份已经属于其他操作")
        if existing.runtime_payload != canonical_json(attribution):
            raise LedgerConflictError("重复尝试不能替换发起人或运行上下文；旧记录缺失归因不能补写")
        return existing
    attempts = list((await session.scalars(select(AttemptRow).where(AttemptRow.operation_key == key)
                                           .order_by(AttemptRow.attempt_number)
                                           .execution_options(populate_existing=True))).all())
    if any(a.status == AttemptStatus.STARTED for a in attempts):
        raise LedgerConflictError("已有未结算尝试，不能并发创建另一次尝试")
    if attempts and not retry:
        raise LedgerConflictError("已有尝试历史，再次尝试必须显式声明 retry")
    row = AttemptRow(attempt_id=attempt_id, operation_key=key, attempt_number=len(attempts) + 1,
                     status=AttemptStatus.STARTED.value, actor_id=attribution.actor_id,
                     runtime_payload=canonical_json(attribution), runtime_digest=input_digest(attribution))
    session.add(row)
    await session.flush()
    return row


async def _attempt(session, attempt_id):
    # 不同终态操作在同一行上排队；锁住后刷新 ORM 缓存以免读到旧状态。
    row = await session.scalar(select(AttemptRow).where(AttemptRow.attempt_id == attempt_id)
                               .with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise LedgerIntegrityError("尝试记录不存在")
    return row


async def settle_attempt(session: AsyncSession, attempt_id: str, status: AttemptStatus, *, code: str) -> AttemptRow:
    """人工/后续执行协调器明确结算失败或不确定；不根据超时猜测外部已失败。"""
    status = AttemptStatus(status)
    TypeAdapter(Identity).validate_python(code)
    if status not in (AttemptStatus.FAILED, AttemptStatus.UNCERTAIN):
        raise ValueError("这里只允许结算 FAILED 或 UNCERTAIN，COMMITTED 必须同时保存产物")
    row = await _attempt(session, attempt_id)
    if row.status == status and row.resolution_code == code:
        return row
    if row.status != AttemptStatus.STARTED:
        raise LedgerConflictError("尝试终态不可改写")
    row.status, row.resolution_code = status.value, code
    row.ended_at = max(datetime.now(timezone.utc), row.started_at)
    await session.flush()
    return row


async def _observation_results(session, operation):
    """解释引用只能指向同计划已提交的结果，且按正式任务顺序一一对应。"""
    results = []
    for execution_id in operation.execution_refs:
        row = await session.scalar(select(ResultRow).join(OperationRow, ResultRow.operation_key == OperationRow.operation_key)
                                   .where(ResultRow.execution_id == execution_id, OperationRow.plan_id == operation.plan_id))
        if row is None:
            raise LedgerIntegrityError("解释引用的执行结果未提交或不属于本计划")
        artifact = _read(row, CommittedArtifact)
        results.append(artifact.execution)
    plan_row = await session.get(AcceptedPlanRow, operation.plan_id)
    plan = _read(plan_row, AcceptedPlan).plan
    for task, result in zip(plan.tasks, results):
        task.status = TaskStatus.COMPLETED if result.status == "SUCCEEDED" else TaskStatus.FAILED
    validate_round_inputs(plan, results)
    return results


async def _validate_artifact(session, operation, artifact):
    if operation.operation_type == OperationType.QUERY_FORMULATION:
        if artifact.query_request is None:
            raise LedgerIntegrityError("查询形成操作必须提交候选请求")
    elif operation.operation_type == OperationType.KNOWLEDGE_EXECUTION:
        result = artifact.execution
        if result is None or result.task_id != operation.task_id or result.capability != CapabilityType.KNOWLEDGE_RETRIEVAL:
            raise LedgerIntegrityError("执行结果与正式任务身份或能力不一致")
        if result.payload is not None:
            if isinstance(operation, OperationInput):
                scope, profile, limit = operation.context.scope, operation.context.index_profile, operation.context.max_top_k
            else:
                scope, profile, limit = operation.scope, operation.index_profile, operation.candidate_request.top_k
            if result.payload.retrieved.snapshot.index_profile != profile:
                raise LedgerIntegrityError("执行结果的 profile 与可信输入不一致")
            candidate, effective = operation.candidate_request, result.payload.request
            expected = candidate.filters.model_dump(mode="json")
            for field, allowed in scope.model_dump(mode="json").items():
                if allowed is None:
                    continue
                supplied = expected[field]
                if field == "document_types":
                    if not allowed:
                        continue
                    if supplied and not set(supplied).issubset(allowed):
                        raise LedgerIntegrityError("候选请求超出可信类型范围")
                    if not supplied:
                        expected[field] = allowed
                elif supplied is not None and supplied != allowed:
                    raise LedgerIntegrityError("候选请求超出可信范围")
                if supplied is None:
                    expected[field] = allowed
            if (candidate.query != effective.query or candidate.top_k != effective.top_k
                    or effective.top_k > limit or effective.filters.model_dump(mode="json") != expected):
                raise LedgerIntegrityError("实际执行请求与候选请求及可信范围不一致")
    else:
        observation = artifact.observation
        if observation is None or observation.execution_refs != operation.execution_refs:
            raise LedgerIntegrityError("解释产物的执行引用与输入不一致")
        results = await _observation_results(session, operation)
        summaries, _ = summarize_executions(results)
        if observation.execution_summaries != summaries:
            raise LedgerIntegrityError("解释系统摘要与真实执行记录不一致")
        if observation.status == "SUCCEEDED":
            validate_observation_draft(KnowledgeObservationDraft(
                execution_refs=observation.execution_refs, findings=observation.findings,
                knowledge_gaps=observation.knowledge_gaps, next_step_suggestions=observation.next_step_suggestions,
                narrative=observation.narrative,
            ), results)


async def commit_result(session: AsyncSession, attempt_id: str, result_id: str, artifact: CommittedArtifact) -> ResultRow:
    """产物与 Attempt.COMMITTED 同事务提交；COMMITTED 不等于业务 SUCCEEDED。"""
    TypeAdapter(Identity).validate_python(result_id)
    artifact = _fresh(artifact)
    attempt = await _attempt(session, attempt_id)
    if attempt.status == AttemptStatus.COMMITTED:
        row = await session.get(ResultRow, attempt.result_id)
        if row is None:
            raise LedgerIntegrityError("已提交尝试缺少产物")
        _read(row, CommittedArtifact)
        if row.result_id != result_id or row.payload != canonical_json(artifact):
            raise LedgerConflictError("已提交结果不能替换身份或载荷")
        return row
    if attempt.status != AttemptStatus.STARTED:
        raise LedgerConflictError("已结算尝试不能再提交产物")
    operation = read_operation(await session.get(OperationRow, attempt.operation_key))
    await _validate_artifact(session, operation, artifact)
    row = ResultRow(result_id=result_id, attempt_id=attempt_id, operation_key=attempt.operation_key,
                    execution_id=artifact.execution.execution_id if artifact.execution else None,
                    observation_id=artifact.observation.observation_id if artifact.observation else None,
                    payload=canonical_json(artifact), input_digest=input_digest(artifact), schema_version=artifact.schema_version)
    session.add(row)
    attempt.status, attempt.result_id = AttemptStatus.COMMITTED.value, result_id
    attempt.ended_at = max(datetime.now(timezone.utc), attempt.started_at)
    await session.flush()
    return row


async def inspect_operation(session: AsyncSession, value: OperationInput | BusinessOperationInput) -> tuple[tuple[AttemptRow, CommittedArtifact | None], ...] | None:
    """None=未登记，空 tuple=已登记未尝试；返回全部历史，不替调用方挑选重试结果。"""
    value = _fresh(value)
    key = operation_key(value)
    row = await session.get(OperationRow, key)
    if row is None:
        return None
    stored = read_operation(row)
    matches = business_projection(stored) == value if isinstance(value, BusinessOperationInput) else stored == value
    if not matches:
        raise LedgerConflictError("同一逻辑操作的输入摘要冲突")
    history = []
    for attempt in await session.scalars(select(AttemptRow).where(AttemptRow.operation_key == key)
                                         .order_by(AttemptRow.attempt_number).execution_options(populate_existing=True)):
        result = await session.get(ResultRow, attempt.result_id) if attempt.result_id else None
        if (attempt.status == AttemptStatus.COMMITTED) != (result is not None):
            raise LedgerIntegrityError("尝试提交状态与产物不一致")
        history.append((attempt, _read(result, CommittedArtifact) if result else None))
    return tuple(history)
