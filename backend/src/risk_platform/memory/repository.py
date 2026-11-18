"""案件持久化适配：分类记忆幂等保存，阶段快照固定；调用方负责事务。"""
import hashlib
import json
from dataclasses import replace
from sqlalchemy import select, text
from risk_platform.workflow import access
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import RoundRecord, canonical_json, input_digest
from risk_platform.workflow.persistence.business_inputs import business_projection
from risk_platform.workflow.persistence.models import ResultRow, OperationRow, RoundRow
from .contracts import MemoryRecord, ContextSnapshot, MemoryScope, MemoryContent
from .models import CaseMemoryRow, ContextSnapshotRow
from .investigation_memory import extract_memory
from .context import assemble_context


def _read(row, contract, version):
    if row is None or row.schema_version != version:
        raise ledger.LedgerIntegrityError("旧版或缺失记忆记录，不能自动转换恢复")
    return ledger._read(row, contract)


def read_context_snapshot(row):
    return _read(row, ContextSnapshot, 6)


def read_memory(row):
    memory = _read(row, MemoryRecord, 3)
    if (memory.memory_id, memory.scope, memory.source_id) != (
            row.memory_id, MemoryScope(kind="INVESTIGATION", id=row.investigation_id), row.source_result_id):
        raise ledger.LedgerIntegrityError("记忆载荷与数据库归属不一致")
    return memory


async def capture_observation(session, result):
    """与 Observation 同事务保存；重复来源复用，失败整体回滚。"""
    stored = await session.get(ResultRow, result.result_id)
    if stored is None:
        raise ledger.LedgerIntegrityError("记忆来源结果缺失")
    artifact = ledger.read_artifact(stored)
    if artifact.observation is None:
        raise ledger.LedgerIntegrityError("记忆来源不是 Observation")
    operation = ledger.read_operation(await session.get(OperationRow, stored.operation_key))
    memory = extract_memory(investigation_id=operation.investigation_id,
        source_result_id=stored.result_id, observation=artifact.observation)
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": memory.memory_id})
    existing = await session.get(CaseMemoryRow, memory.memory_id)
    if existing is not None:
        if read_memory(existing) != memory:
            raise ledger.LedgerConflictError("同一来源不能改写记忆")
        return memory.memory_id
    session.add(CaseMemoryRow(memory_id=memory.memory_id, investigation_id=operation.investigation_id,
        round_id=operation.round_id, source_result_id=stored.result_id, schema_version=3,
        payload=canonical_json(memory), input_digest=input_digest(memory)))
    await session.flush()
    return memory.memory_id


async def _authorize_memory(session, row, runtime):
    """返回已重验来源及当前权限的完整记忆；不相信摘要自身声明。"""
    memory = read_memory(row)
    if memory.scope.id != runtime.investigation.investigation_id:
        raise ledger.LedgerIntegrityError("记忆案件归属不一致")
    round_row = await session.get(RoundRow, row.round_id)
    if round_row is None:
        raise ledger.LedgerIntegrityError("记忆轮次缺失")
    historical_runtime = replace(runtime, round=ledger._read(round_row, RoundRecord))
    from risk_platform.workflow.knowledge_tool import knowledge_execution, knowledge_tool
    from risk_platform.workflow.runtime import InvestigationAction, InvestigationAccessError
    result = await session.get(ResultRow, memory.source_id)
    if result is None:
        raise ledger.LedgerIntegrityError("记忆来源结果缺失")
    operation = ledger.read_operation(await session.get(OperationRow, result.operation_key))
    facts = await access.read_operation_history(session, business_projection(operation), context=historical_runtime)
    matches = [fact.artifact.observation for fact in facts or ()
               if fact.artifact is not None and fact.attempt_id == result.attempt_id]
    if len(matches) != 1 or matches[0] is None:
        raise ledger.LedgerIntegrityError("记忆来源未可靠提交")
    for execution_id in matches[0].execution_refs:
        execution_row = await session.scalar(select(ResultRow).where(ResultRow.execution_id == execution_id))
        execution = ledger.read_artifact(execution_row).execution
        if execution.tool_name in (None, "knowledge_search"):
            trusted = knowledge_execution(historical_runtime, InvestigationAction.READ)
            if knowledge_tool(historical_runtime).authorizer(historical_runtime, action=InvestigationAction.READ)(trusted.actor_id, "knowledge:search", trusted) is not True:
                raise InvestigationAccessError("当前无权读取知识派生记忆")
            break
    expected = extract_memory(investigation_id=memory.scope.id, source_result_id=result.result_id, observation=matches[0])
    if expected != memory:
        raise ledger.LedgerIntegrityError("记忆内容与来源不一致")
    return memory


def _is_projection(selected, original):
    """快照只允许整条省略，不能改写、重排或重复源正文。"""
    if selected.model_dump(exclude={"content"}) != original.model_dump(exclude={"content"}):
        return False
    for name in MemoryContent.model_fields:
        remaining = iter(getattr(original.content, name))
        if any(not any(candidate == value for candidate in remaining)
               for value in getattr(selected.content, name)):
            return False
    return True


async def context_for_stage(session, *, runtime, run_id, stage, max_chars=12000):
    """首次固定上下文；恢复读取原快照，重新授权但不重新选择内容。"""
    await access.authorize_run(session, runtime)
    scope = MemoryScope(kind="INVESTIGATION", id=runtime.investigation.investigation_id)
    identity = "context-" + hashlib.sha256(json.dumps([run_id, stage], separators=(",", ":")).encode()).hexdigest()
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": identity})
    existing = await session.get(ContextSnapshotRow, identity)
    if existing is not None:
        snapshot = read_context_snapshot(existing)
        if (snapshot.run_id, snapshot.stage, snapshot.context.scope) != (run_id, stage, scope):
            raise ledger.LedgerIntegrityError("上下文快照身份不一致")
        for selected in snapshot.context.memories:
            row = await session.get(CaseMemoryRow, selected.memory_id)
            original = await _authorize_memory(session, row, runtime)
            if not _is_projection(selected, original):
                raise ledger.LedgerIntegrityError("上下文内容与记忆来源不一致")
        return snapshot.context
    rows = (await session.execute(select(CaseMemoryRow).join(RoundRow,
        CaseMemoryRow.round_id == RoundRow.round_id).where(
        CaseMemoryRow.investigation_id == scope.id, RoundRow.round_number < runtime.round.round_number
    ).order_by(RoundRow.round_number.desc(), CaseMemoryRow.memory_id))).scalars().all()
    memories = [await _authorize_memory(session, row, runtime) for row in rows]
    context = assemble_context(scope=scope, candidates=memories, max_chars=max_chars)
    snapshot = ContextSnapshot(run_id=run_id, stage=stage, context=context)
    session.add(ContextSnapshotRow(snapshot_id=identity, investigation_id=scope.id,
        run_id=run_id, stage=stage, schema_version=6,
        payload=canonical_json(snapshot), input_digest=input_digest(snapshot)))
    await session.flush()
    return context
