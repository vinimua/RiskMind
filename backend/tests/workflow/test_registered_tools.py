"""显式工具声明与执行分派，不把注册等同于授权。"""

import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from risk_platform.execution import knowledge, tools
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.workflow.contracts import CapabilityType
from test_first_round_execution import state, context
from workflow_test_helpers import runtime_for
from test_observation import execution


def test_tool_definition_has_no_workflow_phase_or_ambiguous_callbacks():
    from dataclasses import fields
    names = {field.name for field in fields(tools.ToolDefinition)}
    assert not {"execution_phase", "model_input", "validate_execution", "handler", "request_model", "name", "description"} & names
    from langchain_core.tools import StructuredTool
    assert isinstance(tools.KNOWLEDGE_SEARCH.tool, StructuredTool)
    assert not hasattr(tools, "execute_registered_tool")
    assert {"execution_limits", "validate_result", "authorize_result"} <= names


def test_tool_definition_requires_explicit_result_protocol():
    from dataclasses import fields
    values = {field.name: getattr(tools.KNOWLEDGE_SEARCH, field.name)
              for field in fields(tools.ToolDefinition)}
    del values["result_model"]
    with pytest.raises(TypeError, match="result_model"):
        tools.ToolDefinition(**values)


def test_tool_definition_rejects_missing_result_projection():
    with pytest.raises(TypeError):
        replace(tools.KNOWLEDGE_SEARCH, to_observation_items=None)


def test_tool_definition_rejects_result_instance_instead_of_model_class():
    with pytest.raises(TypeError):
        replace(tools.KNOWLEDGE_SEARCH, result_model=dict)


def test_registered_schema_has_no_identity_and_cannot_be_mutated_by_caller():
    declarations = [convert_to_openai_tool(tools.KNOWLEDGE_SEARCH.tool)]
    assert declarations[0]["function"]["name"] == KnowledgeSearchTool.name
    schema = declarations[0]["function"]["parameters"]
    assert set(schema["properties"]) == {"query", "top_k", "filters"}
    assert set(schema["required"]) == {"query", "top_k"}
    assert tools.KNOWLEDGE_SEARCH.tool.args_schema.model_config["extra"] == "forbid"
    declarations[0]["function"]["name"] = "forged"
    assert convert_to_openai_tool(tools.KNOWLEDGE_SEARCH.tool)["function"]["name"] == "knowledge_search"
    with pytest.raises(TypeError):
        tools.TOOL_DEFINITIONS["forged"] = tools.KNOWLEDGE_SEARCH


def test_csv_declaration_only_exposes_its_own_request_fields():
    schema = convert_to_openai_tool(tools.CSV_SAMPLE_STATISTICS.tool)["function"]["parameters"]
    assert set(schema["properties"]) == {"dataset_id", "statistic", "window_start", "window_end", "field"}
    assert set(schema["required"]) == {"dataset_id", "statistic", "window_start", "window_end"}
    assert schema["properties"]["statistic"]["enum"] == ["SAMPLE_COUNT", "BAD_LABEL_RATE", "MISSING_RATE"]
    assert schema["properties"]["window_start"]["format"] == "date-time"
    assert tools.CSV_SAMPLE_STATISTICS.tool.args_schema.model_config["extra"] == "forbid"


def test_registered_metrics_capability_still_requires_explicit_binding():
    with pytest.raises(tools.ToolUnavailableError) as failure:
        tools.available_tools(CapabilityType.FACT_RETRIEVAL, runtime_for(state()["planning_input"], context()))
    assert failure.value.code == "TOOL_NOT_BOUND"


@pytest.mark.parametrize("name,capability,code", [
    ("shell_execute", "KNOWLEDGE_RETRIEVAL", "UNKNOWN_TOOL"),
    ("knowledge_search", "COMPUTATION", "UNSUPPORTED_CAPABILITY"),
])
def test_unregistered_or_mismatched_tool_never_executes(monkeypatch, name, capability, code):
    async def forbidden(*_, **__):
        pytest.fail("未知或错能力工具不能进入 handler")
    monkeypatch.setattr(knowledge, "execute_knowledge_search", forbidden)
    task = state(1)["investigation_plan"].tasks[0].model_copy(update={"capability": CapabilityType(capability)})
    with pytest.raises((ValueError, tools.ToolUnavailableError)):
        tools.validate_tool_call(tools.ToolCall(name, {"query": "q", "top_k": 1}), task.capability,
            runtime_for(state()["planning_input"], context()))


def test_registered_dispatch_delegates_to_existing_adapter(monkeypatch):
    expected, seen = execution(empty=True), []
    async def adapter(task, request, **options):
        seen.append((task, request, options))
        return expected
    monkeypatch.setattr(knowledge, "execute_knowledge_search", adapter)
    task, request, trusted = state(1)["investigation_plan"].tasks[0], {"query": "q", "top_k": 1}, context()
    result = asyncio.run(tools.KNOWLEDGE_SEARCH.tool.ainvoke(request,
        config={"configurable": {"task": task, "runtime": runtime_for(state()["planning_input"], trusted)}}))
    assert result == expected.model_copy(update={"tool_name": "knowledge_search", "result_version": "1"})
    assert len(seen) == 1
    assert seen[0][0] == task and seen[0][1].query == request["query"]
    assert seen[0][2]["context"] == trusted
    assert seen[0][2]["authorize"](trusted.actor_id, "knowledge:search", trusted) is True


def test_registration_does_not_bypass_authorization(monkeypatch):
    async def forbidden(*_, **__):
        pytest.fail("权限拒绝不能检索")
    monkeypatch.setattr(knowledge, "retrieve_dense_children", forbidden)
    result = asyncio.run(tools.KNOWLEDGE_SEARCH.tool.ainvoke({"query": "q", "top_k": 1},
        config={"configurable": {"task": state(1)["investigation_plan"].tasks[0],
            "runtime": runtime_for(state()["planning_input"], context(), authorize=lambda *_: False)}}))
    assert result.status == "REJECTED" and result.error.code == "TOOL_NOT_AUTHORIZED"


def test_contract_digest_includes_tool_schema_and_description(monkeypatch):
    original = tools.query_contract_digest("prompt")
    modified = replace(tools.KNOWLEDGE_SEARCH, tool=tools.KNOWLEDGE_SEARCH.tool.model_copy(update={"description": "不同工具说明"}))
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType({modified.tool.name: modified}))
    assert tools.query_contract_digest("prompt") != original


@pytest.mark.parametrize("actor", ["A", "B"])
def test_knowledge_dependencies_are_resolved_from_current_binding(monkeypatch, actor):
    from dataclasses import replace
    from test_runtime import make_runtime
    task = state(1)["investigation_plan"].tasks[0]
    sessions, embedder, milvus = object(), object(), object()
    runtime = make_runtime(state()["planning_input"], context().model_copy(update={"actor_id": actor}))
    binding = replace(runtime.tools[0], session_factory=sessions, embedding_client=embedder, milvus_client=milvus)
    runtime = replace(runtime, tools=(binding,))
    async def adapter(task, request, **options):
        assert options["context"].actor_id == actor
        assert options["session_factory"] is sessions
        assert options["embedding_client"] is embedder
        assert options["milvus_client"] is milvus
        return execution(empty=True)
    monkeypatch.setattr(knowledge, "execute_knowledge_search", adapter)
    result = asyncio.run(tools.KNOWLEDGE_SEARCH.tool.ainvoke({"query": "q", "top_k": 1},
        config={"configurable": {"task": task, "runtime": runtime}}))
    assert result.status == "SUCCEEDED"
    # 不可序列化的客户端不能混入模型声明或已有审计上下文。
    import json
    json.dumps(convert_to_openai_tool(tools.KNOWLEDGE_SEARCH.tool))
    json.dumps(binding.execution.model_dump(mode="json"))
    assert "embedding_client=" not in repr(binding) and "session_factory=" not in repr(binding)

def test_required_tool_mode_is_used_by_request_and_digest(monkeypatch):
    from risk_platform.agents.investigation import InvestigationAgent
    from tool_model_helpers import ToolModelStub, tool_response
    model = ToolModelStub(tool_response())
    agent=InvestigationAgent(None,"test-model",tool_model=model)
    task=state(1)["investigation_plan"].tasks[0]
    call=agent.formulate_tool_call(task,state()["planning_input"],goal="test",
        tools=[tools.KNOWLEDGE_SEARCH.tool],tool_limits={"knowledge_search":{"max_top_k":5}})
    assert call.tool_name=="knowledge_search"
    assert model.calls[0]["tool_choice"]=="required" and model.calls[0]["parallel_tool_calls"] is False
    assert model.calls[0]["extra_body"]=={"thinking":{"type":"disabled"}}
    digest=tools.query_contract_digest("prompt")
    monkeypatch.setattr(tools,"required_tool_options",lambda:{"tool_choice":"required","parallel_tool_calls":False})
    assert tools.query_contract_digest("prompt")!=digest
