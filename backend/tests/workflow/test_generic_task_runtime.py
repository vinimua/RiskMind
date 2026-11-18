from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
from functools import partial
from risk_platform.workflow import phase_execution
"""仅测试通用边界：测试工具不会注册进生产代码，也不代表新增工具能力。"""

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict

from risk_platform.execution import tools
from risk_platform.execution.contracts import ExecutionError, TaskExecutionResult
from risk_platform.workflow import nodes, knowledge_tool, recovery
from risk_platform.workflow.contracts import CapabilityType
from risk_platform.workflow.persistence.contracts import OperationType
from risk_platform.workflow.persistence.repository import LedgerIntegrityError
from test_runtime import make_runtime
from test_first_round_execution import state
from test_observation import current, execution, draft
from workflow_test_helpers import run_phase


@dataclass(frozen=True)
class TestBinding:
    __test__ = False
    name: str

    def validate_binding(self, actor_id, resource):
        assert actor_id == "actor"


class Reference(BaseModel):
    reference_id: str


class ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: str


def install_test_tools(monkeypatch, calls):
    def forbidden(*_, **__):
        pytest.fail("通用任务不能提前要求知识上下文")
    monkeypatch.setattr(knowledge_tool, "knowledge_execution", forbidden)
    definitions = dict(tools.TOOL_DEFINITIONS)
    for name in ("test_read_a", "test_read_b"):
        async def handler(config: RunnableConfig, _name=name, **arguments):
            task = config["configurable"]["task"]
            runtime_context = config["configurable"]["runtime"]
            request = ReadArgs.model_validate(arguments)
            calls.append((_name, request.key, runtime_context.actor_id))
            now = datetime.now(timezone.utc)
            return TaskExecutionResult(execution_id="E-" + _name, task_id=task.task_id,
                capability=task.capability, tool_name=_name, result_version="1", status="FAILED", started_at=now, completed_at=now,
                error=ExecutionError(category="DEPENDENCY", code="TEST_ONLY", message="测试工具的失败事实", retryable=False))
        definitions[name] = tools.ToolDefinition(capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
            tool=StructuredTool.from_function(coroutine=handler, name=name, description="Test read", args_schema=ReadArgs, infer_schema=False),
            execution_limits=lambda _: {"allowed_key": "record"}, validate_request=lambda *_: None,
                    result_model=ReadArgs, to_observation_items=lambda _: [], reference_model=Reference,
            validate_reference=lambda *_: None, authorize_result=lambda *_: True,
            validate_result=lambda *_: None)
    # 不匹配当前任务的能力，即使绑定也不能提供给模型。
    definitions["test_compute"] = replace(definitions["test_read_a"], tool=definitions["test_read_a"].tool.model_copy(update={"name": "test_compute"}), capability=CapabilityType.COMPUTATION)
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType(definitions))


def test_generic_loop_uses_selected_tool_without_knowledge_binding(monkeypatch):
    calls, phases = [], []
    install_test_tools(monkeypatch, calls)
    source = state(1)
    runtime = replace(make_runtime(source["planning_input"]), tools=(TestBinding("test_read_a"), TestBinding("test_read_b"),
        TestBinding("test_compute"), TestBinding("unregistered")))
    class Agent:
        def formulate_tool_call(self, *_args, tools, tool_limits, **_):
            assert [tool.name for tool in tools] == ["test_read_a", "test_read_b"]
            assert set(tool_limits) == {"test_read_a", "test_read_b"}
            return tools_module.ToolCall("test_read_b", {"key": "record"})
    tools_module = tools
    async def phase(kind, produce, **metadata):
        phases.append((kind, metadata.get("tool_name")))
        return await produce()
    result = asyncio.run(nodes.task_execution(source, investigation_agent=Agent(), runtime_context=runtime, query_phase=partial(phase, OperationType.QUERY_FORMULATION), tool_phase=partial(phase, OperationType.TOOL_EXECUTION)))
    assert calls == [("test_read_b", "record", "actor")]
    assert result["task_execution_results"][0].error.code == "TEST_ONLY"
    assert phases[-1] == (OperationType.TOOL_EXECUTION, "test_read_b")


def test_model_cannot_select_registered_but_unbound_tool(monkeypatch):
    calls = []
    install_test_tools(monkeypatch, calls)
    source = state(1)
    runtime = replace(make_runtime(source["planning_input"]), tools=(TestBinding("test_read_a"),))
    class Agent:
        def formulate_tool_call(self, *_args, **_):
            return tools.ToolCall("knowledge_search", {"query": "q", "top_k": 1})
    result = asyncio.run(nodes.task_execution(source, investigation_agent=Agent(), runtime_context=runtime, query_phase=run_phase, tool_phase=run_phase))
    assert result["task_execution_results"][0].error.code == "QUERY_FORMULATION_INVALID"
    assert calls == []


def test_no_bound_tool_rejects_before_model(monkeypatch):
    source = state(1)
    runtime = replace(make_runtime(source["planning_input"]), tools=())
    class Agent:
        def formulate_tool_call(self, *_args, **_):
            pytest.fail("未绑定工具不应调用模型")
    result = asyncio.run(nodes.task_execution(source, investigation_agent=Agent(), runtime_context=runtime, query_phase=run_phase, tool_phase=run_phase))
    assert result["task_execution_results"][0].status == "REJECTED"
    assert result["task_execution_results"][0].error.code == "TOOL_NOT_BOUND"


def test_observation_needs_generic_identity_not_knowledge_binding(monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("解释已有结果不应提前取得知识执行配置")
    monkeypatch.setattr(knowledge_tool, "knowledge_execution", forbidden)
    results = [execution()]
    source = current(results)
    runtime = replace(make_runtime(source["planning_input"]), tools=())
    class Agent:
        def observe_round(self, *_, **__):
            from risk_platform.workflow.observation import ObservationDraft
            return ObservationDraft.model_validate(draft(results))
    output = asyncio.run(nodes.observation(source, investigation_agent=Agent(), runtime_context=runtime, observation_phase=run_phase))
    assert output["observation"].status == "SUCCEEDED"


def test_stage_entrypoints_have_no_kind_dispatch():
    import inspect
    assert not hasattr(phase_execution, "ledger_phase_executor")
    for entry in (phase_execution.query_phase, phase_execution.tool_phase, phase_execution.observation_phase):
        assert "kind" not in inspect.signature(entry).parameters


@pytest.mark.parametrize("committed", [True, False])
def test_tool_result_log_requires_reliable_phase_return(monkeypatch, caplog, committed):
    from types import SimpleNamespace
    from test_observation import execution
    result = execution()
    monkeypatch.setattr(phase_execution, "operation_key", lambda _: "stable-operation")

    async def phase(*args, **kwargs):
        if not committed:
            raise RuntimeError("commit failed")
        return SimpleNamespace(execution=result)

    monkeypatch.setattr(phase_execution, "_phase", phase)
    call = SimpleNamespace(runtime=make_runtime(state(1)["planning_input"]),
        execution=SimpleNamespace(identity=SimpleNamespace(investigation_id="I", round_id="R", plan_id="P")))
    execute = partial(phase_execution.tool_phase, sessions=None, call=call,
        task_id="T0", candidate={"query": "q", "top_k": 1}, tool_name="knowledge_search",
        capability=CapabilityType.KNOWLEDGE_RETRIEVAL)
    caplog.set_level("INFO", logger=phase_execution.__name__)
    # 其他测试的 Alembic 日志配置会禁用已存在的 logger；本例只验收阶段事件。
    monkeypatch.setattr(phase_execution.logger, "disabled", False)
    if committed:
        assert asyncio.run(execute(None)) == result
    else:
        with pytest.raises(RuntimeError):
            asyncio.run(execute(None))
    events = [r for r in caplog.records if r.message == "tool_result"]
    assert len(events) == int(committed)
    if events:
        assert events[0].execution_status == "SUCCEEDED"
        assert events[0].tool_name == "knowledge_search"
        assert not hasattr(events[0], "query") and not hasattr(events[0], "payload")


@pytest.mark.parametrize("name", [None, {}, ["knowledge_search"]])
def test_malformed_tool_name_is_invalid_request_not_internal_failure(name):
    source = state(1)
    runtime = make_runtime(source["planning_input"])
    with pytest.raises(ValueError):
        tools.validate_tool_call(tools.ToolCall(name, {}), CapabilityType.KNOWLEDGE_RETRIEVAL, runtime)
