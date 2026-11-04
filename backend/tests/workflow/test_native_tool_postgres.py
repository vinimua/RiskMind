"""真实 PG/checkpointer + 原生 tool_calls 结构替身；不调用付费模型。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow import recovery
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
    agent.formulate_knowledge_query = real_agent.formulate_knowledge_query
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


def test_changed_tool_contract_cannot_silently_resume_existing_run(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            events = list(agent.events)
            monkeypatch.setattr(recovery, "query_contract_digest", lambda _: "0" * 64)
            with pytest.raises(ledger.LedgerConflictError):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert agent.events == events
    asyncio.run(scenario())
