"""8D-2：唯一业务循环通过可替换的阶段执行机制运行。"""

import asyncio

from risk_platform.execution import knowledge as registered_knowledge

import pytest

from risk_platform.workflow import nodes
from risk_platform.workflow.persistence.contracts import OperationType
from test_first_round_execution import Agent, context, empty_dependencies, state


def test_business_nodes_have_no_default_direct_executor():
    assert not hasattr(nodes, "direct_phase")
    current, events = state(1), []
    with pytest.raises(TypeError, match="execute_phase"):
        asyncio.run(nodes.task_execution(current, investigation_agent=Agent(current, events),
            execution_context=context(), authorize=lambda *_: True))
    with pytest.raises(TypeError, match="execute_phase"):
        asyncio.run(nodes.observation(current, investigation_agent=object(), execution_context=context()))
    assert events == []


def test_shared_loop_and_observation_use_phase_executor_in_business_order(monkeypatch):
    current, events, phases = state(), [], []
    empty_dependencies(monkeypatch, events)
    async def execute(kind, produce, **metadata):
        phases.append((kind, metadata.get("task_id")))
        return await produce()
    async def scenario():
        result = await nodes.task_execution(current, investigation_agent=Agent(current, events),
            execution_context=context(), authorize=lambda *_: True, execute_phase=execute)
        update = await nodes.observation(result, investigation_agent=object(),
            execution_context=context(), execute_phase=execute)
        assert update["observation"].status == "INCONCLUSIVE"
    asyncio.run(scenario())
    assert phases == [
        (OperationType.QUERY_FORMULATION, "T0"), (OperationType.KNOWLEDGE_EXECUTION, "T0"),
        (OperationType.QUERY_FORMULATION, "T1"), (OperationType.KNOWLEDGE_EXECUTION, "T1"),
        (OperationType.OBSERVATION, None),
    ]


def test_failed_query_has_no_execution_phase(monkeypatch):
    current, events, phases = state(1), [], []
    empty_dependencies(monkeypatch, events)
    async def execute(kind, produce, **metadata):
        phases.append(kind)
        return await produce()
    result = asyncio.run(nodes.task_execution(current, investigation_agent=Agent(current, events, ValueError("private")),
        execution_context=context(), authorize=lambda *_: pytest.fail("失败 Query 不得进入工具"), execute_phase=execute))
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
            execution_context=context(), authorize=lambda *_: True, execute_phase=execute))
    assert committed == [OperationType.QUERY_FORMULATION]
