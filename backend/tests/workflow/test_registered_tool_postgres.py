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
    async def handler(task, request, *, runtime_context):
        events.append("execute")
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="numeric-E", task_id=task.task_id,
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

    definition = tools.ToolDefinition(name="test_numeric_read", capability=capability,
        description="Test-only read of a versioned numeric result", request_model=Args,
        result_model=NumericResult, handler=handler,
        execution_limits=lambda _: {"dataset": "dataset-v1"}, validate_request=validate_request,
        to_observation_items=lambda result: [{"reference_id": "metric", "content": result.model_dump(mode="json")}],
        reference_model=NumericReference, validate_reference=validate_reference,
        validate_result=validate_result,
        authorize_result=lambda runtime, result: result.payload is None or result.payload["dataset"] == "dataset-v1")
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType(dict(tools.TOOL_DEFINITIONS) | {definition.name: definition}))
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
        state.update(await nodes.task_execution(state, investigation_agent=Agent(events), runtime_context=runtime, execute_phase=run_phase))
        state.update(await nodes.observation(state, investigation_agent=Agent(events), runtime_context=runtime, execute_phase=run_phase))
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
