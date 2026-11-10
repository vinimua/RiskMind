"""真实 PG/checkpointer + 原生 tool_calls 结构替身；不调用付费模型。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow import recovery
from risk_platform.workflow import access, runner as runner_module
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.models import AttemptRow, OperationRow
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import Agent, context, resources, retrieval, seed


@pytest.mark.parametrize("selected_name", ["knowledge_search", "unregistered_shell"])
def test_native_selection_is_committed_and_resume_does_not_repeat_model_or_tool(database, monkeypatch, selected_name):
    agent, sdk_calls = Agent(), []
    retrieval(monkeypatch, agent)
    def create(**payload):
        sdk_calls.append(payload)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            content=None, tool_calls=[SimpleNamespace(type="function", function=SimpleNamespace(
                name=selected_name, arguments=json.dumps({"query": "Query native", "top_k": 1})))]))])
    real_agent = InvestigationAgent(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), agent.model)
    agent.formulate_tool_call = real_agent.formulate_tool_call
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert len(sdk_calls) == 1
            assert sdk_calls[0]["tool_choice"] == "required" and sdk_calls[0]["parallel_tool_calls"] is False
            events = list(agent.events)
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert second == first and agent.events == events and len(sdk_calls) == 1
            result = first["task_execution_results"][0]
            async with sessions() as session:
                operations = list(await session.scalars(select(OperationRow)))
                assert all(ledger.read_operation(row).tool_name == "knowledge_search" for row in operations)
                attempts = list(await session.scalars(select(AttemptRow)))
                if selected_name == "knowledge_search":
                    assert result.status == "SUCCEEDED" and len(attempts) == 3
                    assert "search:native" in agent.events
                else:
                    assert result.error.code == "QUERY_FORMULATION_INVALID" and len(attempts) == 2
                    assert not any(event.startswith("search:") for event in agent.events)
    asyncio.run(scenario())


def test_changed_tool_contract_does_not_relabel_or_rerun_committed_history(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            events = list(agent.events)
            from risk_platform.workflow import phase_execution
            monkeypatch.setattr(phase_execution, "query_contract_digest", lambda _: "0" * 64)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert agent.events == events
    asyncio.run(scenario())


def test_resource_authorization_can_load_case_without_any_knowledge_binding(database):
    from risk_platform.workflow.access import load_runtime_context
    from risk_platform.workflow.runtime import InvestigationAction, InvestigationRuntimeContext
    agent = Agent()
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, _, runner):
            await seed(sessions, count=1)
            async with sessions() as session:
                runtime = await load_runtime_context(session, investigation_id="I", round_id="R", actor_id="A",
                    tools=(), authorize_resource=lambda *_: True, action=InvestigationAction.CONTINUE)
                trusted = await access.authorize_run(session, runtime)
                assert isinstance(trusted, InvestigationRuntimeContext) and trusted.tools == ()
                assert not hasattr(trusted, "index_profile")
    asyncio.run(scenario())


def test_bound_knowledge_dependencies_reach_retrieval_without_entering_checkpoint(database, monkeypatch):
    from dataclasses import replace
    from risk_platform.execution import knowledge
    from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
    from risk_platform.knowledge.context_resolution import ContextResolutionResult
    from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot
    agent = Agent()
    embedder, milvus = object(), object()
    seen = []
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            runtime = await context(sessions)
            runtime = replace(runtime, tools=(replace(runtime.tools[0], session_factory=sessions,
                embedding_client=embedder, milvus_client=milvus),))
            async def retrieve(request, **options):
                assert options["session_factory"] is sessions
                assert options["embedding_client"] is embedder and options["milvus_client"] is milvus
                seen.append("retrieve")
                return DenseChildRetrievalResult(snapshot=RetrievalRouteSnapshot(index_profile="parent-child-v1",
                    collection="bound-physical", route_version=1), hits=())
            async def resolve(result, **options):
                assert options["session_factory"] is sessions
                return ContextResolutionResult(snapshot=result.snapshot, contexts=())
            monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
            monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
            result = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=runtime))
            assert result["task_execution_results"][0].status == "SUCCEEDED" and seen == ["retrieve"]
            checkpoint = await runner._graph.aget_state({"configurable": {"thread_id": result["run_id"]}})
            payload = json.dumps(checkpoint.values)
            assert "embedding_client" not in payload and "milvus_client" not in payload and "session_factory" not in payload
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=runtime))
            assert second == result and seen == ["retrieve"]
    asyncio.run(scenario())
