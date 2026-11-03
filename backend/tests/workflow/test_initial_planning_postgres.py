"""图内初始规划：真实 PG/checkpointer，候选与接受之间的崩溃窗口。"""

import asyncio
from dataclasses import replace
import threading
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from risk_platform.workflow import recovery
from risk_platform.workflow.contracts import CapabilityType, InvestigationPlan, InvestigationTask, TaskStatus
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord
from risk_platform.workflow.persistence.models import AcceptedPlanRow, AttemptRow
from risk_platform.workflow.persistence.planning_models import PlanningAttemptRow, PlanningAcceptanceRow, PlanningRunRow
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow
from risk_platform.workflow.runtime import InvestigationAccessError
from test_recovery_postgres import Agent, ProcessLost, resources, context, retrieval
from test_recovery_postgres import seed as seed_accepted
from risk_platform.workflow.persistence.contracts import AcceptedPlan
from test_runtime import planning
from test_ledger_postgres import database, pytestmark


class Planner(Agent):
    def initial_plan(self, source):
        self.events.append("initial-plan")
        assert source.user_query.model_id == "M"
        return InvestigationPlan(goal="调查历史背景", tasks=[InvestigationTask(task_id=f"T{i}", objective="查历史",
            capability="KNOWLEDGE_RETRIEVAL", expected_result="线索") for i in range(2)])


async def seed_round(sessions):
    async with sessions.begin() as session:
        await ledger.register_investigation(session, InvestigationRecord(investigation_id="I", created_by="A", planning_input=planning()))
        await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))


async def call(runner, sessions, *, actor="A", policy=None):
    return await runner.ainvoke(context=InvestigationInvocation(runtime=await context(sessions, actor)), policy=policy)


def test_no_plan_id_starts_planning_inside_graph_then_reuses_everything(database, monkeypatch):
    agent = Planner()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            first = await call(runner, sessions)
            assert first["run_id"].startswith("round-") and first["plan_id"].startswith("plan-")
            assert agent.events == ["initial-plan", "query:T0", "search:T0", "query:T1", "search:T1", "observation"]
            events = list(agent.events)
            second = await call(runner, sessions, actor="B")
            assert first == second and agent.events == events
            async with sessions() as session:
                assert len((await session.scalars(select(AcceptedPlanRow))).all()) == 1
                assert await session.get(AcceptedPlanRow, first["plan_id"]) is not None
                assert len((await session.scalars(select(AttemptRow))).all()) == 5
                fact = (await session.scalars(select(PlanningAttemptRow))).one()
                assert fact.status == "COMMITTED" and fact.actor_id == "A"
                acceptance = (await session.scalars(select(PlanningAcceptanceRow))).one()
                assert acceptance.attempt_id == fact.attempt_id and acceptance.actor_id == "A"
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["candidate", "acceptance"])
def test_committed_candidate_or_acceptance_survives_missing_node_checkpoint(database, monkeypatch, boundary):
    agent = Planner()
    retrieval(monkeypatch, agent)
    name = "_planning_candidate" if boundary == "candidate" else "prepare_plan"
    original, crashed = getattr(recovery, name), {"done": False}
    async def interrupt(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not crashed["done"]:
            crashed["done"] = True
            raise ProcessLost("planning phase committed before node checkpoint")
        return result
    monkeypatch.setattr(recovery, name, interrupt)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(ProcessLost):
                await call(runner, sessions)
        async with resources(database, agent) as (sessions, saver, runner):
            result = await call(runner, sessions, actor="B")
            assert result["round_complete"] and agent.events.count("initial-plan") == 1
            async with sessions() as session:
                fact = (await session.scalars(select(PlanningAttemptRow))).one()
                row = (await session.scalars(select(AcceptedPlanRow))).one()
                assert fact.status == "COMMITTED" and fact.actor_id == "A"
                import json
                assert json.loads(fact.candidate)["plan"] == json.loads(row.payload)["plan"]
                assert (await session.scalars(select(PlanningAcceptanceRow))).one().actor_id == ("B" if boundary == "candidate" else "A")
    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["duplicate", "capability", "completed", "empty", "blank"])
def test_invalid_initial_plan_is_not_accepted_or_executed(database, monkeypatch, bad):
    agent = Planner()
    original = agent.initial_plan
    def invalid(source):
        plan = original(source)
        if bad == "duplicate": plan.tasks[1].task_id = plan.tasks[0].task_id
        elif bad == "capability": plan.tasks[0].capability = CapabilityType.COMPUTATION
        elif bad == "completed": plan.tasks[0].status = TaskStatus.COMPLETED
        elif bad == "empty": plan.tasks = []
        else: plan.tasks[0].objective = " "
        return plan
    agent.initial_plan = invalid
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(recovery.PlanningPreparationError):
                await call(runner, sessions)
            with pytest.raises(recovery.RecoveryBlockedError):
                await call(runner, sessions)
            assert agent.events == ["initial-plan"]
            async with sessions() as session:
                assert (await session.scalars(select(AcceptedPlanRow))).all() == []
                assert (await session.scalars(select(AttemptRow))).all() == []
                assert (await session.scalars(select(InvestigationRunRow))).all() == []
                assert (await session.scalars(select(PlanningAttemptRow))).one().status == "FAILED"
            agent.initial_plan = original
            retrieval(monkeypatch, agent)
            assert (await call(runner, sessions, actor="B", policy=RecoveryPolicy(retry_failed_attempts=True)))["round_complete"]
    asyncio.run(scenario())


def test_unknown_planning_interruption_requires_explicit_retry_and_preserves_actor(database, monkeypatch):
    agent = Planner()
    original = agent.initial_plan
    def interrupted(*_):
        raise ProcessLost("API outcome unknown")
    agent.initial_plan = interrupted
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(ProcessLost): await call(runner, sessions)
            with pytest.raises(recovery.RecoveryBlockedError): await call(runner, sessions, actor="B")
            agent.initial_plan = original
            retrieval(monkeypatch, agent)
            assert (await call(runner, sessions, actor="B", policy=RecoveryPolicy(retry_uncertain=True)))["round_complete"]
            async with sessions() as session:
                facts = (await session.scalars(select(PlanningAttemptRow).order_by(PlanningAttemptRow.attempt_number))).all()
                assert [(row.status, row.actor_id) for row in facts] == [("UNCERTAIN", "A"), ("COMMITTED", "B")]
    asyncio.run(scenario())


def test_revoked_continue_after_model_never_accepts_or_executes(database):
    agent = Planner()
    allowed = {"value": True}
    original = agent.initial_plan
    def revoke(source):
        result = original(source)
        allowed["value"] = False
        return result
    agent.initial_plan = revoke
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            runtime = await context(sessions)
            runtime = replace(runtime, authorize_resource=lambda *_: allowed["value"])
            with pytest.raises(InvestigationAccessError):
                await runner.ainvoke(context=InvestigationInvocation(runtime=runtime))
            async with sessions() as session:
                assert (await session.scalars(select(AcceptedPlanRow))).all() == []
                assert (await session.scalars(select(AttemptRow))).all() == []
    asyncio.run(scenario())


def test_concurrent_initial_planning_is_blocked_by_same_round_lock(database, monkeypatch):
    agent = Planner()
    retrieval(monkeypatch, agent)
    started, release = threading.Event(), threading.Event()
    original = agent.initial_plan
    def blocked(source):
        started.set()
        if not release.wait(15): raise TimeoutError()
        return original(source)
    agent.initial_plan = blocked
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            first = asyncio.create_task(call(runner, sessions))
            try:
                assert await asyncio.to_thread(started.wait, 10)
                with pytest.raises(recovery.RecoveryBlockedError):
                    await call(runner, sessions, actor="B")
            finally:
                release.set()
            assert (await first)["round_complete"] and agent.events.count("initial-plan") == 1
    asyncio.run(scenario())


def test_lost_planning_lock_fences_old_candidate_after_new_actor_finishes(database, monkeypatch):
    old_agent, new_agent = Planner(), Planner()
    retrieval(monkeypatch, new_agent)
    started, release, observed = threading.Event(), threading.Event(), {}
    original = old_agent.initial_plan
    def blocked(source):
        started.set()
        if not release.wait(20): raise TimeoutError()
        return original(source)
    old_agent.initial_plan = blocked
    async def scenario():
        async with resources(database, old_agent, setup=True) as (sessions, saver, old):
            await seed_round(sessions)
            invoke = old._graph.ainvoke
            async def capture(*args, context, **kwargs):
                observed["pid"] = context.recovery.guard.backend_pid
                return await invoke(*args, context=context, **kwargs)
            monkeypatch.setattr(old._graph, "ainvoke", capture)
            first = asyncio.create_task(call(old, sessions))
            try:
                assert await asyncio.to_thread(started.wait, 10)
                async with sessions.begin() as session:
                    await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": observed["pid"]})
                async with resources(database, new_agent) as (fresh_sessions, _, fresh):
                    assert (await call(fresh, fresh_sessions, actor="B", policy=RecoveryPolicy(retry_uncertain=True)))["round_complete"]
            finally:
                release.set()
            with pytest.raises(recovery.RecoveryFencedError): await first
            async with sessions() as session:
                facts = (await session.scalars(select(PlanningAttemptRow).order_by(PlanningAttemptRow.attempt_number))).all()
                assert [(row.status, row.actor_id) for row in facts] == [("UNCERTAIN", "A"), ("COMMITTED", "B")]
    asyncio.run(scenario())


def test_planning_history_and_acceptance_are_immutable_in_postgres(database, monkeypatch):
    agent = Planner()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            await call(runner, sessions)
            for statement in (
                "UPDATE investigation_planning_runs SET input_digest=repeat('0',64)",
                "UPDATE investigation_planning_attempts SET candidate='{}' WHERE status='COMMITTED'",
                "DELETE FROM investigation_planning_attempts",
                "UPDATE investigation_planning_acceptances SET actor_id='forged'",
            ):
                with pytest.raises(DBAPIError):
                    async with sessions.begin() as session:
                        await session.execute(text(statement))
    asyncio.run(scenario())


def test_existing_accepted_plan_is_loaded_without_initial_model_even_without_plan_id(database, monkeypatch):
    agent = Agent()  # initial_plan 会直接使测试失败。
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_accepted(sessions)
            assert (await call(runner, sessions))["round_complete"]
            async with sessions() as session:
                assert (await session.scalars(select(PlanningRunRow))).all() == []
    asyncio.run(scenario())


def test_multiple_accepted_plans_are_not_silently_selected(database):
    agent = Agent()
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_accepted(sessions)
            async with sessions.begin() as session:
                original = ledger._read(await session.get(AcceptedPlanRow, "P"), AcceptedPlan)
                await ledger.accept_plan(session, original.model_copy(update={"plan_id": "P2", "plan_version": 2}))
            with pytest.raises(ledger.LedgerConflictError, match="多份正式计划"):
                await call(runner, sessions)
            assert agent.events == []
    asyncio.run(scenario())


def test_acceptance_cannot_bind_different_plan_content_to_committed_candidate(database, monkeypatch):
    agent = Planner()
    original = recovery._planning_candidate
    async def interrupt(*args):
        result = await original(*args)
        raise ProcessLost("candidate committed")
    monkeypatch.setattr(recovery, "_planning_candidate", interrupt)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(ProcessLost): await call(runner, sessions)
            with pytest.raises(DBAPIError, match="differs from candidate"):
                async with sessions.begin() as session:
                    root = (await session.scalars(select(PlanningRunRow))).one()
                    fact = (await session.scalars(select(PlanningAttemptRow))).one()
                    from risk_platform.workflow.persistence.planning_contracts import PlanningCandidate
                    candidate = PlanningCandidate.model_validate_json(fact.candidate)
                    candidate.plan.goal = "人工替换后的不同计划"
                    await ledger.accept_plan(session, AcceptedPlan(investigation_id="I", round_id="R",
                        plan_id=root.plan_id, plan_version=1, plan=candidate.plan))
                    session.add(PlanningAcceptanceRow(run_id=root.run_id, plan_id=root.plan_id,
                        investigation_id="I", round_id="R", attempt_id=fact.attempt_id, actor_id="A"))
                    await session.flush()
            async with sessions() as session:
                assert (await session.scalars(select(AcceptedPlanRow))).all() == []
    asyncio.run(scenario())


def test_actual_process_exit_after_candidate_commit_resumes_without_replanning(database, monkeypatch):
    agent = Planner()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
        child = r'''
import asyncio, os
from test_initial_planning_postgres import Planner, resources, call
from risk_platform.workflow import recovery
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
original = recovery._planning_candidate
async def exit_after_candidate(*args):
    await original(*args)
    os._exit(23)
recovery._planning_candidate = exit_after_candidate
async def main():
    async with resources(os.environ["PLANNING_TEST_DATABASE_URL"], Planner()) as (sessions, saver, runner):
        await call(runner, sessions)
asyncio.run(main())
'''
        environment = dict(os.environ)
        environment["PLANNING_TEST_DATABASE_URL"] = database.render_as_string(hide_password=False)
        environment["PYTHONPATH"] = str(Path(__file__).parent) + os.pathsep + environment.get("PYTHONPATH", "")
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", child],
            env=environment, capture_output=True, timeout=35)
        assert result.returncode == 23, result.stderr.decode(errors="replace")
        async with resources(database, agent) as (sessions, saver, runner):
            assert (await call(runner, sessions, actor="B"))["round_complete"]
            assert "initial-plan" not in agent.events
    asyncio.run(scenario())
