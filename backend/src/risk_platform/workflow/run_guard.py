"""运行独占权与代次护栏；只管理 claim、锁和 generation，不决定图的节点顺序。"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4
from sqlalchemy import select, text
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import AcceptedPlan, canonical_json, input_digest
from risk_platform.workflow.persistence.models import AcceptedPlanRow
from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, RecoveryPolicy, RunClaim, read_run_identity
from risk_platform.workflow.persistence.planning_models import PlanningAcceptanceRow, PlanningRunRow
from risk_platform.workflow.persistence.recovery_models import InvestigationRunClaimRow, InvestigationRunRow
from risk_platform.workflow.access import authorize_run


class RecoveryBlockedError(RuntimeError):
    """不确定/歧义/并发运行需要显式处理，不能自动假装未执行。"""


class RecoveryFencedError(RecoveryBlockedError):
    """当前运行已失去独占权，不接受过期 worker 的产物。"""


@dataclass
class RunGuard:
    """会话锁证明当前进程仍持有运行权；generation 阻止旧代次结果提交。"""

    connection: object
    backend_pid: int
    run_id: str
    generation: int
    # 本次持锁者身份；规划阶段可用它区分同一占用与已退出的旧占用。
    owner_id: str = field(default_factory=lambda: uuid4().hex)

    async def check_lock(self):
        if self.connection.closed or self.connection.invalidated:
            raise RecoveryFencedError("运行锁连接已失效")
        try:
            row = (await self.connection.execute(text("SELECT pg_backend_pid(), EXISTS (SELECT 1 FROM pg_locks "
                "WHERE pid=pg_backend_pid() AND locktype='advisory' AND granted)"))).one()
        except Exception:
            raise RecoveryFencedError("无法确认本次运行的独占权") from None
        if row[0] != self.backend_pid or not row[1]:
            raise RecoveryFencedError("本次运行已失去数据库会话锁")

    async def check(self, session):
        await self.check_lock()
        current = await session.scalar(select(InvestigationRunRow).where(InvestigationRunRow.run_id == self.run_id)
                                        .with_for_update().execution_options(populate_existing=True))
        if current is None or current.current_generation != self.generation:
            raise RecoveryFencedError("运行代次已经变化，拒绝过期提交")


@dataclass(frozen=True)
class RunExecutionContext:
    """claim 后创建的内部执行控制；不是 Agent 输入，也不会进入检查点。"""

    identity: RunIdentity
    policy: RecoveryPolicy
    guard: RunGuard
# identity           本次运行身份，不包含阶段配置
# policy             重试与历史结果选择规则
# guard              运行权检查

# 应用提供 runtime
#     ↓
# Runner 调用 _claim()
#     ↓
# _claim 取得运行锁，创建 RunExecutionContext
#     ↓
# Runner 将它填入新包装的 execution 字段
#     ↓
# 把包装传给 LangGraph
#     ↓
# 节点通过 require_run_context(runtime) 取出并检查
@asynccontextmanager
async def _claim(engine, sessions, identity, context, policy, *, allow_planning=False):
    # 专用自动提交连接持会话锁；外部调用期间不持长数据库事务/行锁。
    connection = await engine.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        pid, acquired = (await connection.execute(text("SELECT pg_backend_pid(), "
            "pg_try_advisory_lock(hashtextextended(:key,0))"), {"key": f"riskmind:first-round:{identity.run_id}"})).one()
        if not acquired:
            raise RecoveryBlockedError("同一轮正在运行，拒绝并发接续")
        async with sessions.begin() as session:
            trusted = await authorize_run(session, context)
            plan_row = await session.get(AcceptedPlanRow, identity.plan_id)
            if plan_row is not None and (plan_row.investigation_id, plan_row.round_id) != (identity.investigation_id, identity.round_id):
                raise ledger.LedgerIntegrityError("恢复必须从同一调查、轮次的已接受计划开始")
            preparation = await session.get(PlanningRunRow, identity.run_id)
            if plan_row is None and not allow_planning:
                raise ledger.LedgerIntegrityError("恢复必须从同一调查、轮次的已接受计划开始")
            generation = 0
            if plan_row is not None:
                if preparation is not None and await session.get(PlanningAcceptanceRow, identity.run_id) is None:
                    raise ledger.LedgerIntegrityError("规划记录缺少正式接受来源，不能跳过候选校验")
                generation = await _bind_execution_run(session, identity, plan_row, trusted, policy)
        yield RunExecutionContext(identity, policy, RunGuard(connection, pid, identity.run_id, generation))
    finally:
        # 不把持会话锁的连接放回池；连接关闭即释放全部重入锁，进程死亡也由 PG 释放。
        try:
            await asyncio.shield(connection.invalidate())
        finally:
            await connection.close()


async def _bind_execution_run(session, identity, plan_row, trusted, policy):
    """AcceptedPlan 已存在后，原有正式 Run/Claim 才能建立；调用方负责提交。"""
    plan = ledger._read(plan_row, AcceptedPlan)
    if any(task.capability != "KNOWLEDGE_RETRIEVAL" for task in plan.plan.tasks):
        raise ledger.LedgerIntegrityError("首轮恢复仅支持正式知识任务")
    row = await session.get(InvestigationRunRow, identity.run_id)
    if row is None:
        row, _ = await ledger._insert(session, InvestigationRunRow, identity.run_id, identity, run_id=identity.run_id,
            investigation_id=identity.investigation_id, round_id=identity.round_id, plan_id=identity.plan_id)
    elif read_run_identity(row) != identity:
        raise ledger.LedgerConflictError("运行身份不一致")
    generation = (row.current_generation or 0) + 1
    claim = RunClaim(run_id=identity.run_id, generation=generation, actor_id=trusted.actor_id, policy=policy)
    session.add(InvestigationRunClaimRow(run_id=identity.run_id, generation=generation, actor_id=trusted.actor_id,
        schema_version=1, payload=canonical_json(claim), input_digest=input_digest(claim)))
    row.current_generation = generation
    await session.flush()
    return generation
