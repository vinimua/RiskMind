"""真实 PG：Observation 原子派生记忆、快照恢复和权限。"""
import asyncio
from dataclasses import replace
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import Agent, resources, seed, context, retrieval
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import RoundRecord
from risk_platform.workflow.persistence.models import ResultRow
from risk_platform.memory.models import CaseMemoryRow, ContextSnapshotRow
from risk_platform.memory.repository import capture_observation, context_for_stage
from risk_platform.workflow.runtime import InvestigationAccessError


def test_memory_is_committed_once_and_snapshot_is_fixed_and_authorized(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            runtime = await context(sessions)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=runtime))
            async with sessions.begin() as session:
                row = (await session.scalars(select(CaseMemoryRow))).one()
                result = (await session.scalars(select(ResultRow).where(ResultRow.observation_id == first["observation"].observation_id))).one()
                assert await capture_observation(session, result) == row.memory_id
                # 同轮输入排除自身，避免部分执行影响同轮后续模型。
                empty = await context_for_stage(session, runtime=runtime, run_id="test-same", stage="PLANNING")
                assert not empty.memories
                second_round = RoundRecord(investigation_id="I", round_id="R2", round_number=2)
                await ledger.register_round(session, second_round)
            later = replace(runtime, round=second_round)
            async with sessions.begin() as session:
                saved = await context_for_stage(session, runtime=later, run_id="future", stage="PLANNING")
                assert saved.memories and saved.memories[0].source_id == result.result_id
            async with sessions.begin() as session:
                # 同阶段不能因新的预算而无声改变已经固定的输入。
                assert await context_for_stage(session, runtime=later, run_id="future", stage="PLANNING", max_chars=512) == saved
                assert len((await session.scalars(select(CaseMemoryRow))).all()) == 1
            revoked_tool = replace(later.tools[0], authorize=lambda *_: False)
            with pytest.raises(InvestigationAccessError):
                async with sessions.begin() as session:
                    await context_for_stage(session, runtime=replace(later, tools=(revoked_tool,)),
                        run_id="future", stage="PLANNING")
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    await session.execute(text("UPDATE case_memories SET payload='{}'"))
            events = list(agent.events)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=runtime))
            assert agent.events == events
    asyncio.run(scenario())


def test_memory_failure_rolls_back_observation_result_and_selection(database, monkeypatch):
    from risk_platform.workflow import phase_execution
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def fail(*_):
        raise ValueError("memory write failed")
    monkeypatch.setattr(phase_execution, "capture_observation", fail)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            with pytest.raises(ValueError, match="memory write failed"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            async with sessions() as session:
                assert not (await session.scalars(select(ResultRow).where(ResultRow.observation_id.is_not(None)))).all()
                assert not (await session.scalars(select(CaseMemoryRow))).all()
    asyncio.run(scenario())


def test_history_reaches_planning_and_tool_request_without_reexecuting_old_round(database, monkeypatch):
    from risk_platform.workflow.contracts import InvestigationPlan, InvestigationTask
    class MemoryAgent(Agent):
        seen = []
        def initial_plan(self, source, *, memory_context=None):
            assert memory_context.memories
            self.seen.append(("planning", memory_context))
            return InvestigationPlan(goal="补充调查", tasks=[InvestigationTask(task_id="NEXT", objective="补查历史",
                capability="KNOWLEDGE_RETRIEVAL", expected_result="线索")])
        def formulate_tool_call(self, task, *args, **kwargs):
            if task.task_id == "NEXT":
                assert kwargs["memory_context"].memories
                self.seen.append(("query", kwargs["memory_context"]))
            return super().formulate_tool_call(task, *args, **kwargs)
    agent = MemoryAgent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            runtime = await context(sessions)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=runtime))
            next_round = RoundRecord(investigation_id="I", round_id="R2", round_number=2)
            async with sessions.begin() as session:
                await ledger.register_round(session, next_round)
            second = await runner.ainvoke(context=InvestigationInvocation(runtime=replace(runtime, round=next_round)))
            assert second["round_complete"] and second["investigation_status"] == "OPEN"
            assert [name for name, _ in agent.seen] == ["planning", "query"]
            assert all("NEXT" not in text for _, ctx in agent.seen for m in ctx.memories for text in m.content.execution_summaries)
            events = list(agent.events)
            await runner.ainvoke(context=InvestigationInvocation(runtime=replace(runtime, round=next_round)))
            assert agent.events == events
            async with sessions() as session:
                assert len((await session.scalars(select(CaseMemoryRow))).all()) == 2
    asyncio.run(scenario())


def test_old_snapshot_is_preserved_but_refused(database):
    import hashlib
    import json
    agent = Agent()
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            runtime = await context(sessions)
            identity = "context-" + hashlib.sha256(json.dumps(["legacy", "PLANNING"], separators=(",", ":")).encode()).hexdigest()
            async with sessions.begin() as session:
                session.add(ContextSnapshotRow(snapshot_id=identity, investigation_id="I", run_id="legacy", stage="PLANNING",
                    schema_version=1, payload="{}", input_digest="0"*64))
            with pytest.raises(ledger.LedgerIntegrityError, match="旧版"):
                async with sessions.begin() as session:
                    await context_for_stage(session, runtime=runtime, run_id="legacy", stage="PLANNING")
            async with sessions() as session:
                row = await session.get(ContextSnapshotRow, identity)
                assert row.schema_version == 1 and row.payload == "{}"
    asyncio.run(scenario())
