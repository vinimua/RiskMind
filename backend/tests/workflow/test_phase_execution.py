"""8D-2：唯一业务循环通过可替换的阶段执行机制运行。"""

from functools import partial

import asyncio
from workflow_test_helpers import runtime_for, tool_call

from risk_platform.execution import knowledge as registered_knowledge

import pytest

from risk_platform.workflow import nodes
from risk_platform.workflow.persistence.contracts import OperationType
from test_first_round_execution import Agent, context, empty_dependencies, state


@pytest.mark.parametrize("change", ["tool_name", "result_version", "request"])
def test_structured_tool_result_is_validated_at_ledger_boundary(change):
    from risk_platform.workflow.persistence import repository as ledger
    from risk_platform.workflow.persistence.business_inputs import ToolOperationInput
    from risk_platform.workflow.persistence.recovery_contracts import ToolArtifact
    from risk_platform.execution.tools import KNOWLEDGE_SEARCH
    from risk_platform.execution.contracts import KnowledgeSearchToolRequest
    from test_observation import execution
    source = state(1)
    runtime = runtime_for(source["planning_input"], context())
    result = execution(empty=True).model_copy(update={"tool_name": "knowledge_search", "result_version": "1"})
    candidate = result.payload.request
    if change == "request":
        candidate = KnowledgeSearchToolRequest(query="a different request", top_k=1)
    else:
        result = result.model_copy(update={change: "wrong"})
    operation = ToolOperationInput(investigation_id="I", round_id="R", plan_id="P",
        task_id=result.task_id, operation_type=OperationType.TOOL_EXECUTION,
        candidate_request=candidate.model_dump(mode="json"), tool_name="knowledge_search",
        capability=result.capability, tool_limits={"knowledge_search": KNOWLEDGE_SEARCH.execution_limits(runtime)})
    with pytest.raises(ValueError):
        asyncio.run(ledger._validate_artifact(None, operation, ToolArtifact(execution=result)))


def test_business_nodes_have_no_default_direct_executor():
    assert not hasattr(nodes, "direct_phase")
    current, events = state(1), []
    with pytest.raises(TypeError, match="query_phase"):
        asyncio.run(nodes.task_execution(current, investigation_agent=Agent(current, events),
            runtime_context=runtime_for(current["planning_input"], context(), authorize=lambda *_: True)))
    with pytest.raises(TypeError, match="observation_phase"):
        asyncio.run(nodes.observation(current, investigation_agent=object(), runtime_context=runtime_for(current["planning_input"], context())))
    assert events == []


def test_shared_loop_and_observation_use_phase_executor_in_business_order(monkeypatch):
    current, events, phases = state(), [], []
    empty_dependencies(monkeypatch, events)
    async def execute(kind, produce, **metadata):
        phases.append((kind, metadata.get("task_id")))
        return await produce()
    async def scenario():
        result = await nodes.task_execution(current, investigation_agent=Agent(current, events),
            runtime_context=runtime_for(current["planning_input"], context(), authorize=lambda *_: True), query_phase=partial(execute, OperationType.QUERY_FORMULATION), tool_phase=partial(execute, OperationType.TOOL_EXECUTION))
        update = await nodes.observation({**current, **result}, investigation_agent=object(),
            runtime_context=runtime_for(current["planning_input"], context()), observation_phase=partial(execute, OperationType.OBSERVATION))
        assert update["observation"].status == "INCONCLUSIVE"
    asyncio.run(scenario())
    assert phases == [
        (OperationType.QUERY_FORMULATION, "T0"), (OperationType.TOOL_EXECUTION, "T0"),
        (OperationType.QUERY_FORMULATION, "T1"), (OperationType.TOOL_EXECUTION, "T1"),
        (OperationType.OBSERVATION, None),
    ]


def test_failed_query_has_no_execution_phase(monkeypatch):
    current, events, phases = state(1), [], []
    empty_dependencies(monkeypatch, events)
    async def execute(kind, produce, **metadata):
        phases.append(kind)
        return await produce()
    result = asyncio.run(nodes.task_execution(current, investigation_agent=Agent(current, events, ValueError("private")),
        runtime_context=runtime_for(current["planning_input"], context(), authorize=lambda *_: pytest.fail("失败 Query 不得进入工具")), query_phase=partial(execute, OperationType.QUERY_FORMULATION), tool_phase=partial(execute, OperationType.TOOL_EXECUTION)))
    assert phases == [OperationType.QUERY_FORMULATION]
    assert result["task_execution_results"][0].error.code == "QUERY_FORMULATION_INVALID"


def test_task_identity_is_validated_before_phase_can_commit(monkeypatch):
    from datetime import datetime, timezone
    from risk_platform.execution.contracts import ExecutionError, TaskExecutionResult
    current, events, committed = state(1), [], []
    async def wrong(*_, **__):
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="E", task_id="OTHER", capability="KNOWLEDGE_RETRIEVAL",
            status="FAILED", started_at=now, completed_at=now,
            error=ExecutionError(category="INTERNAL", code="X", message="错误", retryable=False))
    monkeypatch.setattr(registered_knowledge, "execute_knowledge_search", wrong)
    async def execute(kind, produce, **metadata):
        output = await produce()
        committed.append(kind)
        return output
    with pytest.raises(ValueError, match="身份不一致"):
        asyncio.run(nodes.task_execution(current, investigation_agent=Agent(current, events),
            runtime_context=runtime_for(current["planning_input"], context(), authorize=lambda *_: True), query_phase=partial(execute, OperationType.QUERY_FORMULATION), tool_phase=partial(execute, OperationType.TOOL_EXECUTION)))
    assert committed == [OperationType.QUERY_FORMULATION]
