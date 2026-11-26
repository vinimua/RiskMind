from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
"""异构只读测试工具：同一任务循环、预算、账本和检查点，不注册进生产环境。"""
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from risk_platform.execution import tools
from risk_platform.execution.contracts import TaskExecutionResult
from risk_platform.workflow import nodes
from risk_platform.workflow.contracts import InitialPlanningInput, UserQuery, InvestigationTask, InvestigationPlan, CapabilityType
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.observation import ObservationDraft, complete_observation, project_tool_results
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord, AcceptedPlan, OperationType
from risk_platform.workflow.persistence.models import ResultRow, OperationRow
from risk_platform.workflow.access import load_runtime_context
from risk_platform.workflow.runtime import InvestigationAction
from test_ledger_postgres import database
from test_recovery_postgres import resources
from workflow_test_helpers import run_phase


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset: str


class NumericResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dataset: str
    value: float


class NumericReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reference_id: str
    value: float


@dataclass(frozen=True)
class Binding:
    name: str = "test_numeric_read"

    def validate_binding(self, actor, resource):
        if actor != "A" or resource.model_id != "M":
            raise PermissionError("Test grant does not cover resource")


def install(monkeypatch, events, capability=CapabilityType.COMPUTATION):
    async def handler(config: RunnableConfig, **arguments):
        task = config["configurable"]["task"]
        request = Args.model_validate(arguments)
        events.append("execute")
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="numeric-" + task.task_id, task_id=task.task_id,
            capability=task.capability, tool_name="test_numeric_read", result_version="1",
            status="SUCCEEDED", started_at=now, completed_at=now,
            payload={"dataset": request.dataset, "value": 0.31})

    def validate_request(runtime, request):
        if request.dataset != "dataset-v1":
            raise PermissionError("Dataset outside grant")

    def validate_result(request, limits, result):
        if result.dataset != request.dataset or result.dataset != limits["dataset"]:
            raise ValueError("Result belongs to another dataset")

    def validate_reference(reference, result):
        ref = NumericReference.model_validate(reference)
        if ref.reference_id != "metric" or ref.value != result.value:
            raise ValueError("Invented metric reference")

    definition = tools.ToolDefinition(capability=capability,
        tool=StructuredTool.from_function(coroutine=handler, name="test_numeric_read", description="Test numeric read", args_schema=Args, infer_schema=False),
        result_model=NumericResult,
        execution_limits=lambda _: {"dataset": "dataset-v1"}, validate_request=validate_request,
        to_observation_items=lambda result: [{"reference_id": "metric", "content": result.model_dump(mode="json")}],
        reference_model=NumericReference, validate_reference=validate_reference,
        validate_result=validate_result,
        authorize_result=lambda runtime, result: result.payload is None or result.payload["dataset"] == "dataset-v1")
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType(dict(tools.TOOL_DEFINITIONS) | {definition.tool.name: definition}))
    return definition


class Agent:
    model = "test-only-model"

    def __init__(self, events):
        self.events = events

    def formulate_tool_call(self, *args, **kwargs):
        self.events.append("query")
        return tools.ToolCall("test_numeric_read", Args(dataset="dataset-v1"))

    def observe_round(self, plan, results, **kwargs):
        self.events.append("observation")
        assert project_tool_results(results)[0]["items"][0]["content"]["value"] == 0.31
        return ObservationDraft(findings=[dict(statement="Test dataset value is 0.31; not a root-cause conclusion",
            sources=[dict(execution_id=results[0].execution_id, reference={"reference_id": "metric", "value": 0.31})])],
            unresolved_questions=[], action="STOP", stop_reason="Test query answered")


def test_numeric_tool_uses_same_execution_and_observation_nodes(monkeypatch):
    from test_runtime import make_runtime
    from dataclasses import replace
    events = []
    install(monkeypatch, events)
    source = InitialPlanningInput(user_query=UserQuery(query="Read numeric result", model_id="M"),
                                  available_capabilities=["COMPUTATION"])
    task = InvestigationTask(task_id="T", objective="Read value", expected_result="Numeric result", capability="COMPUTATION")
    state = dict(planning_input=source, investigation_plan=InvestigationPlan(goal="Read value", tasks=[task]))
    runtime = replace(make_runtime(source), actor_id="A", tools=(Binding(),))
    async def scenario():
        state.update(await nodes.task_execution(state, investigation_agent=Agent(events), runtime_context=runtime, query_phase=run_phase, tool_phase=run_phase))
        state.update(await nodes.observation(state, investigation_agent=Agent(events), runtime_context=runtime, observation_phase=run_phase))
        assert state["observation"].status == "SUCCEEDED"
        assert state["observation"].knowledge_scope == "TOOL_RESULTS_ONLY"
        bad = Agent(events).observe_round(None, state["task_execution_results"])
        bad.findings[0].sources[0].reference["value"] = 999
        with pytest.raises(ValueError):
            complete_observation(state["task_execution_results"], bad)
    asyncio.run(scenario())


@pytest.mark.skipif(os.environ.get("RUN_POSTGRES_INTEGRATION") != "1", reason="requires isolated PostgreSQL schemas")
def test_mixed_registry_can_choose_existing_knowledge_tool_and_recover(database, monkeypatch):
    from dataclasses import replace
    from test_recovery_postgres import Agent as KnowledgeAgent, seed, context, retrieval
    events = []
    install(monkeypatch, events, capability=CapabilityType.KNOWLEDGE_RETRIEVAL)
    agent = KnowledgeAgent()
    retrieval(monkeypatch, agent)

    async def bound(sessions):
        runtime = await context(sessions)
        return replace(runtime, tools=(*runtime.tools, Binding()))

    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await bound(sessions)))
            assert first["task_execution_results"][0].tool_name == "knowledge_search"
            assert first["observation"].status == "SUCCEEDED"
        before = list(agent.events)
        async with resources(database, agent) as (sessions, saver, runner):
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await bound(sessions)))
            assert second == first and agent.events == before
    asyncio.run(scenario())


@pytest.mark.skipif(os.environ.get("RUN_POSTGRES_INTEGRATION") != "1", reason="requires isolated PostgreSQL schemas")
def test_numeric_tool_persists_and_reopens_without_execution(database, monkeypatch):
    events = []
    install(monkeypatch, events)
    source = InitialPlanningInput(user_query=UserQuery(query="Read numeric result", model_id="M"), available_capabilities=["COMPUTATION"])
    async def runtime(sessions):
        async with sessions() as session:
            return await load_runtime_context(session, investigation_id="I", round_id="R", actor_id="A",
                authorize_resource=lambda *_: True, tools=(Binding(),), action=InvestigationAction.CONTINUE)

    async def scenario():
        async with resources(database, Agent(events), setup=True) as (sessions, saver, runner):
            async with sessions.begin() as session:
                await ledger.register_investigation(session, InvestigationRecord(investigation_id="I", created_by="A", planning_input=source))
                await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
                await ledger.accept_plan(session, AcceptedPlan(investigation_id="I", round_id="R", plan_id="P", plan_version=1,
                    plan=InvestigationPlan(goal="Read numeric result", tasks=[InvestigationTask(task_id="T", objective="Read", expected_result="Value", capability="COMPUTATION")])))
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
            assert first["observation"].status == "SUCCEEDED"
            async with sessions() as session:
                assert len(list(await session.scalars(select(ResultRow)))) == 3
                assert {row.operation_type for row in await session.scalars(select(OperationRow))} == {"QUERY_FORMULATION", "TOOL_EXECUTION", "OBSERVATION"}
        before = list(events)
        async with resources(database, Agent(events)) as (sessions, saver, runner):
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
            assert second == first and events == before
    asyncio.run(scenario())


@pytest.mark.skipif(os.environ.get("RUN_POSTGRES_INTEGRATION") != "1", reason="requires isolated PostgreSQL schemas")
@pytest.mark.parametrize("interrupt_transition", [False, True, "after_plan", "budget", "task_budget", "permission", "concurrency"])
def test_observation_drives_successor_and_recovers_transition(database, monkeypatch, interrupt_transition):
    from risk_platform.workflow import planning
    from risk_platform.workflow.persistence.models import RoundProgressionRow, RoundRow
    from test_recovery_postgres import ProcessLost
    events = []
    install(monkeypatch, events)
    source = InitialPlanningInput(user_query=UserQuery(query="Read numeric result", model_id="M"), available_capabilities=["COMPUTATION"])
    permissions = {"allow_next": interrupt_transition != "permission"}
    if interrupt_transition == "task_budget":
        from contextlib import asynccontextmanager
        from dataclasses import replace
        from risk_platform.workflow import runner as runner_module
        original_claim = runner_module._claim
        @asynccontextmanager
        async def limited_claim(*args, **kwargs):
            async with original_claim(*args, **kwargs) as claim:
                yield replace(claim, max_tasks=3)
        monkeypatch.setattr(runner_module, "_claim", limited_claim)
    class LoopAgent(Agent):
        from pathlib import Path
        from risk_platform.agents import investigation as agent_module
        from risk_platform.agents.skills import SkillDirectory
        skill_directory = SkillDirectory(Path(agent_module.__file__).with_name("skill_library"))
        def initial_plan(self, source, **kwargs):
            events.append("plan-next")
            assert kwargs["previous_observations"][0].unresolved_questions == ("Q2", "Q3")
            assert kwargs["previous_tasks"][0]["status"] == "SUCCEEDED"
            return InvestigationPlan(goal="Resolve Q2 and Q3", tasks=[
                InvestigationTask(task_id="T" + str(len(kwargs["previous_observations"])) + "-2", objective="Resolve Q2", expected_result="Value", capability="COMPUTATION"),
                InvestigationTask(task_id="T" + str(len(kwargs["previous_observations"])) + "-3", objective="Resolve Q3", expected_result="Value", capability="COMPUTATION")])
        def observe_round(self, plan, results, **kwargs):
            draft = super().observe_round(plan, results, **kwargs)
            if results[0].task_id == "T" or interrupt_transition in ("budget", "task_budget"):
                return draft.model_copy(update=dict(action="CONTINUE", stop_reason=None, unresolved_questions=("Q2", "Q3")))
            assert kwargs["previous_observations"][0].unresolved_questions == ("Q2", "Q3")
            return draft
    original = planning.advance_round
    interrupted = False
    async def advance(*args, **kwargs):
        nonlocal interrupted
        update = await original(*args, **kwargs)
        if interrupt_transition is True and not interrupted and not update["round_complete"]:
            interrupted = True
            raise ProcessLost()
        if interrupt_transition == "concurrency" and not interrupted and not update["round_complete"]:
            from risk_platform.workflow.run_guard import RecoveryBlockedError
            interrupted = True
            async with resources(database, LoopAgent(events)) as (sessions, saver, contender):
                async with sessions() as session:
                    link = await session.get(RoundProgressionRow, "R")
                    successor = await load_runtime_context(session, investigation_id="I",
                        round_id=link.next_round_id, actor_id="A",
                        authorize_resource=lambda *_: True, tools=(Binding(),), action=InvestigationAction.CONTINUE)
                with pytest.raises(RecoveryBlockedError):
                    await contender.ainvoke(context=InvestigationInvocation(runtime=successor))
        return update
    monkeypatch.setattr(planning, "advance_round", advance)
    original_prepare = planning.prepare_plan
    async def prepare(*args, **kwargs):
        nonlocal interrupted
        update = await original_prepare(*args, **kwargs)
        if interrupt_transition == "after_plan" and not interrupted and kwargs["runtime"].round.round_number == 2:
            interrupted = True
            raise ProcessLost()
        return update
    monkeypatch.setattr(planning, "prepare_plan", prepare)
    async def runtime(sessions):
        async with sessions() as session:
            return await load_runtime_context(session, investigation_id="I", round_id="R", actor_id="A",
                authorize_resource=lambda actor, action, resource: resource.round_id == "R" or permissions["allow_next"], tools=(Binding(),), action=InvestigationAction.CONTINUE)
    async def scenario():
        async with resources(database, LoopAgent(events), setup=True) as (sessions, saver, runner):
            async with sessions.begin() as session:
                await ledger.register_investigation(session, InvestigationRecord(investigation_id="I", created_by="A", planning_input=source))
                await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
                await ledger.accept_plan(session, AcceptedPlan(investigation_id="I", round_id="R", plan_id="P", plan_version=1,
                    plan=InvestigationPlan(goal="Read", tasks=[InvestigationTask(task_id="T", objective="Read", expected_result="Value", capability="COMPUTATION")])))
            if interrupt_transition is True or interrupt_transition == "after_plan":
                with pytest.raises(ProcessLost):
                    await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
            elif interrupt_transition == "permission":
                from risk_platform.workflow.runtime import InvestigationAccessError
                with pytest.raises(InvestigationAccessError):
                    await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
                async with sessions() as session:
                    assert len(list(await session.scalars(select(RoundRow)))) == 1
                    assert not list(await session.scalars(select(RoundProgressionRow)))
                permissions["allow_next"] = True
            else:
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
        async with resources(database, LoopAgent(events)) as (sessions, saver, runner):
            result = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
            assert result["observation"].action == ("CONTINUE" if interrupt_transition in ("budget", "task_budget") else "STOP")
            assert result["stop_reason"]
            assert len(result["task_execution_results"]) == 2
            assert events.count("execute") == (5 if interrupt_transition == "budget" else 3)
            assert events.count("plan-next") == (2 if interrupt_transition == "budget" else 1)
            async with sessions() as session:
                assert len(list(await session.scalars(select(RoundRow)))) == (3 if interrupt_transition == "budget" else 2)
                assert len(list(await session.scalars(select(RoundProgressionRow)))) == (3 if interrupt_transition == "budget" else 2)
                assert len(list(await session.scalars(select(ResultRow)))) == (13 if interrupt_transition == "budget" else 8)
                from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow
                from risk_platform.workflow.persistence.models import AcceptedPlanRow
                final_run = await session.get(InvestigationRunRow, result["run_id"])
                origin = await session.get(InvestigationRunRow, result["root_run_id"])
                assert final_run.plan_id == result["plan_id"] and origin.plan_id == "P"
                accepted = ledger._read(await session.get(AcceptedPlanRow, "P"), AcceptedPlan)
                assert accepted.plan.tasks[0].task_id == "T" and accepted.plan.tasks[0].status == "PENDING"
            before = list(events)
            again = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
            assert again == result and events == before
    asyncio.run(scenario())
