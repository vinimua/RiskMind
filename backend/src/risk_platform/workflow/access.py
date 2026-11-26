"""8B 受控账本入口：读历史与继续执行分别鉴权，不调度或恢复节点。"""

from dataclasses import dataclass
from datetime import datetime

from risk_platform.workflow.knowledge_tool import knowledge_execution
from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.workflow.runtime import (
    InvestigationAccessError, InvestigationAction, InvestigationRuntimeContext, check_runtime,
)
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.business_inputs import AttemptAttribution, BusinessOperationInput, ToolOperationInput, ToolAttemptAttribution, business_projection
from risk_platform.workflow.persistence.contracts import (
    CommittedArtifact, InvestigationRecord, OperationType, RoundRecord, canonical_json, input_digest,
)
from risk_platform.workflow.persistence.models import InvestigationRow, OperationRow, RoundRow


@dataclass(frozen=True)
class AttemptFact:
    attempt_id: str
    attempt_number: int
    status: str
    resolution_code: str | None
    started_at: datetime
    ended_at: datetime | None
    actor_id: str | None
    attribution: AttemptAttribution | None
    artifact: CommittedArtifact | None
    # None 明确表示旧数据没有独立尝试归因，不能拿 Operation actor 补写。


@dataclass(frozen=True)
class AttemptReceipt:
    attempt_id: str
    status: str
    actor_id: str | None


async def load_runtime_context(session, *, investigation_id, round_id, actor_id, tools=(),
                               authorize_resource, action: InvestigationAction,
                               timeout_seconds=30.0) -> InvestigationRuntimeContext:
# 它们按主键取得：
# - 案件记录：案件 ID、创建者、原始调查输入等。
# - 轮次记录：轮次 ID、所属案件、第几轮等。
# session.get() 可能使用当前 Session 已加载的对象，因此不保证每次调用都会重新发送 SQL。
    
    investigation = await session.get(InvestigationRow, investigation_id)
    round_record = await session.get(RoundRow, round_id)
    if investigation is None or round_record is None:
        raise InvestigationAccessError("调查或轮次不可访问")
    context = InvestigationRuntimeContext(
        investigation=ledger._read(investigation, InvestigationRecord),
        round=ledger._read(round_record, RoundRecord),
        actor_id=actor_id, tools=tools, timeout_seconds=timeout_seconds,
        authorize_resource=authorize_resource,
    )
    check_runtime(context, action)
    return context


async def _bound_context(session, context, action):
    trusted = check_runtime(context, action)
    current = await load_runtime_context(session, investigation_id=context.investigation.investigation_id,
        round_id=context.round.round_id, actor_id=context.actor_id, tools=context.tools,
        authorize_resource=context.authorize_resource, action=action, timeout_seconds=context.timeout_seconds)
    if current.investigation != context.investigation or current.round != context.round:
        raise InvestigationAccessError("运行上下文与持久化调查事实不一致")
    return trusted
# 不过，_bound_context() 不只调用权限函数，它还核对：
# 案件与轮次是否匹配；
# 传入的案件事实是否与 PostgreSQL 记录一致；
# 已配置工具的身份、资源归属是否正确（不要求存在知识绑定）


async def _knowledge_bound_context(session, context, action):
    """当前知识账本专用适配，不再由通用资源授权隐式要求知识工具。"""
    await _bound_context(session, context, action)
    return knowledge_execution(context, action)


def _belongs(value, context):
    if (value.investigation_id, value.round_id) != (context.investigation.investigation_id, context.round.round_id):
        raise InvestigationAccessError("不能跨调查或轮次访问操作")


def _scope_covers(scope: KnowledgeFilters, filters: KnowledgeFilters):
    for key in ("model_id", "document_id", "document_version"):
        allowed = getattr(scope, key)
        if allowed is not None and getattr(filters, key) != allowed:
            raise InvestigationAccessError("当前权限范围不能覆盖历史或请求数据")
    if scope.document_types and (not filters.document_types or not set(filters.document_types) <= set(scope.document_types)):
        raise InvestigationAccessError("当前权限类型范围不能覆盖历史或请求数据")


def _check_execution(result, trusted):
    if result.payload is None:
        return
    payload = result.payload
    if payload.retrieved.snapshot.index_profile != trusted.index_profile or payload.request.top_k > trusted.max_top_k:
        raise InvestigationAccessError("历史执行 profile 或数量超出当前允许范围")
    _scope_covers(trusted.scope, payload.request.filters)
    for item in payload.retrieved.hits:
        hit = item.hit
        _scope_covers(trusted.scope, KnowledgeFilters(model_id=hit.model_id, document_id=hit.source.document_id,
            document_version=hit.source.document_version, document_types=[hit.document_type]))


def _check_registered_result(result, context):
    if result.tool_name in (None, "knowledge_search"):
        if result.payload is None:
            return  # 请求形成失败，没有工具数据可泄露；操作归属仍由调用方核验。
        _check_execution(result, knowledge_execution(context, InvestigationAction.READ))
        return
    from risk_platform.execution.tools import tool_definition
    definition = tool_definition(result.tool_name)
    if not any(binding.name == result.tool_name for binding in context.tools):
        raise InvestigationAccessError("历史结果工具当前未授权")
    if definition.authorize_result is None or definition.authorize_result(context, result) is not True:
        raise InvestigationAccessError("当前权限不能读取此工具结果")


async def _check_registered_operation(session, value, context):
    from risk_platform.execution.tools import tool_definition
    _belongs(value, context)
    for name, recorded in value.tool_limits.items():
        definition = tool_definition(name)
        if value.capability is not None and definition.capability != value.capability:
            raise InvestigationAccessError("工具能力不属于当前正式任务")
        if not any(binding.name == name for binding in context.tools):
            raise InvestigationAccessError("工具当前未绑定")
        if definition.stage_limits(context) != recorded:
            raise InvestigationAccessError("工具范围或输入限制已变化，不能沿用原记录")
        if value.candidate_request is not None:
            request = definition.tool.args_schema.model_validate(value.candidate_request)
            definition.validate_request(context, request)
    if value.operation_type == OperationType.OBSERVATION:
        for result in await ledger._observation_results(session, value):
            _check_registered_result(result, context)


async def _check_business(session, value, trusted, *, continuing):
    if value.operation_type == OperationType.KNOWLEDGE_EXECUTION:
        if trusted.index_profile != value.index_profile or value.candidate_request.top_k > trusted.max_top_k:
            raise InvestigationAccessError("业务执行 profile 或数量超出本次范围")
        # 确定的候选 + 稳定业务 scope，可在调用前证明生效范围；不查路由或执行检索。
        filters = value.candidate_request.filters.model_dump()
        for key in ("model_id", "document_id", "document_version"):
            fixed = getattr(value.scope, key)
            if fixed is not None:
                if filters[key] is not None and filters[key] != fixed:
                    raise ledger.LedgerConflictError("候选请求与稳定业务范围冲突")
                filters[key] = fixed
        if value.scope.document_types:
            if filters["document_types"] and not set(filters["document_types"]) <= set(value.scope.document_types):
                raise ledger.LedgerConflictError("候选请求超出稳定类型范围")
            filters["document_types"] = filters["document_types"] or value.scope.document_types
        _scope_covers(trusted.scope, KnowledgeFilters(**filters))
    elif value.operation_type == OperationType.QUERY_FORMULATION:
        if continuing and value.max_top_k != trusted.max_top_k:
            raise ledger.LedgerConflictError("查询形成的 max_top_k 是稳定模型输入，不能因恢复静默修改")
    else:
        for result in await ledger._observation_results(session, value):
            _check_execution(result, trusted)


async def register_authorized_operation(session, value: BusinessOperationInput, *, context):
    if isinstance(value, ToolOperationInput):
        await _bound_context(session, context, InvestigationAction.CONTINUE)
        await _check_registered_operation(session, value, context)
        row = await ledger.register_operation(session, value, created_by=context.actor_id)
        return row.operation_key
    trusted = await _knowledge_bound_context(session, context, InvestigationAction.CONTINUE)
    value = BusinessOperationInput.model_validate_json(value.model_dump_json())
    _belongs(value, context)
    await _check_business(session, value, trusted, continuing=True)
    row = await ledger.register_operation(session, value, created_by=trusted.actor_id)
    return row.operation_key  # 不向仅有 CONTINUE 权限的调用方泄露历史载荷。


async def start_authorized_attempt(session, key: str, attempt_id: str, *, context, retry=False):
    row = await session.get(OperationRow, key)
    if row is not None and row.schema_version == 3:
        await _bound_context(session, context, InvestigationAction.CONTINUE)
        stored = ledger.read_operation(row)
        await _check_registered_operation(session, stored, context)
        attribution = ToolAttemptAttribution(investigation_id=stored.investigation_id, round_id=stored.round_id,
                                            actor_id=context.actor_id, tool_limits=stored.tool_limits)
        attempt = await ledger.start_attempt(session, key, attempt_id, retry=retry, attribution=attribution)
        return AttemptReceipt(attempt.attempt_id, attempt.status, attempt.actor_id)
    trusted = await _knowledge_bound_context(session, context, InvestigationAction.CONTINUE)
    row = await session.get(OperationRow, key)
    if row is None:
        raise ledger.LedgerIntegrityError("操作尚未登记")
    stored = ledger.read_operation(row)  # v1 先按原始版本与完整摘要验证。
    _belongs(stored, context)
    await _check_business(session, business_projection(stored), trusted, continuing=True)
    attribution = AttemptAttribution(investigation_id=stored.investigation_id, round_id=stored.round_id,
                                     actor_id=trusted.actor_id, execution_context=trusted)
    attempt = await ledger.start_attempt(session, key, attempt_id, retry=retry, attribution=attribution)
    return AttemptReceipt(attempt.attempt_id, attempt.status, attempt.actor_id)


async def read_operation_history(session, expected: BusinessOperationInput, *, context):
    generic = isinstance(expected, ToolOperationInput)
    trusted = (await _bound_context(session, context, InvestigationAction.READ) if generic else
               await _knowledge_bound_context(session, context, InvestigationAction.READ))
    expected = type(expected).model_validate_json(expected.model_dump_json())
    _belongs(expected, context)
    if generic:
        await _check_registered_operation(session, expected, context)
    else:
        await _check_business(session, expected, trusted, continuing=False)
    history = await ledger.inspect_operation(session, expected)
    if history is None:
        return None
    output = []
    for attempt, artifact in history:
        attribution = None
        if attempt.runtime_payload is not None:
            import json
            contract = ToolAttemptAttribution if json.loads(attempt.runtime_payload).get("schema_version") == 2 else AttemptAttribution
            attribution = contract.model_validate_json(attempt.runtime_payload)
            if (input_digest(attribution) != attempt.runtime_digest or canonical_json(attribution) != attempt.runtime_payload
                    or attribution.actor_id != attempt.actor_id):
                raise ledger.LedgerIntegrityError("尝试身份审计载荷损坏")
        elif attempt.actor_id is not None or attempt.runtime_digest is not None:
            raise ledger.LedgerIntegrityError("尝试身份审计记录不完整")
        if artifact is not None:
            if artifact.execution is not None:
                if generic:
                    _check_registered_result(artifact.execution, context)
                else:
                    _check_execution(artifact.execution, trusted)
            elif artifact.observation is not None:
                # 旧 v1/v2 均需重验支撑执行的当前可读范围，不因已有解释就绕过。
                for result in await ledger._observation_results(session, expected):
                    if generic:
                        _check_registered_result(result, context)
                    else:
                        _check_execution(result, trusted)
        output.append(AttemptFact(attempt.attempt_id, attempt.attempt_number, attempt.status, attempt.resolution_code,
                                  attempt.started_at, attempt.ended_at, attempt.actor_id, attribution, artifact))
    return tuple(output)


async def authorize_run(session, context):
    """加载 canonical 归属并分别检查 CONTINUE 与 READ；二者不能互相推出。

    只有 READ：允许查看，但不能继续运行；只有 CONTINUE：仍不满足读取历史的权限。
    持久化执行需要两者都明确通过，不因检查顺序而赋予另一项权限。
    """
    # 顺序不赋予权限包含关系：恢复读取和继续始终分别显式检查。
    await _bound_context(session, context, InvestigationAction.CONTINUE)
    return await _bound_context(session, context, InvestigationAction.READ)
# 数据库外键只能保证写入记录之间的关联合法，不能单独保证“本次调用者提供的 runtime 就对应这些记录”，因此调用边界仍需要核对。

# 不过，“归属校验有必要”不等于“当前所有重复读取都必要”：
# - 入口及恢复时，加载并核对数据库正式记录，很有价值。
# - 等待模型后，重新检查当前权限有价值。
# - 同一事务中反复加载同一案件、轮次，通常可以合并。
# - 如果案件、轮次记录已被保证不可变，后续是否每次都重读，应按明确的信任边界决定。
# 所以这项保护应该保留；当前 authorize_run() 把归属校验与权限检查绑在一起，确实容易造成重复，也增加了阅读成本。它们可以在实现上分清，不能仅因为函数叫“鉴权”，就认定其中每次查询都不可省。
