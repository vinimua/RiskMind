from langchain_core.utils.function_calling import convert_to_openai_tool
import json

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.tools import TOOL_DEFINITIONS
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationTask, UserQuery
from tool_model_helpers import ToolModelStub, tool_response


def inputs():
    task = InvestigationTask(task_id="T", objective="查历史", expected_result="历史知识", capability="KNOWLEDGE_RETRIEVAL")
    source = InitialPlanningInput(user_query=UserQuery(query="调查", model_id="M"), available_capabilities=["KNOWLEDGE_RETRIEVAL"])
    return task, source


def choose(model):
    task, source = inputs()
    agent = InvestigationAgent(None, "test", tool_model=model)
    return agent.formulate_tool_call(task, source, goal="调查背景",
        tools=[d.tool for d in TOOL_DEFINITIONS.values() if d.capability == task.capability], tool_limits={"knowledge_search": {"max_top_k": 5}})


def test_model_binds_selected_tools_schema_and_returns_typed_request():
    model = ToolModelStub(tool_response(arguments={"query": "标签成熟", "top_k": 3}))
    call = choose(model)
    assert call.tool_name == "knowledge_search" and call.arguments.top_k == 3
    assert len(model.calls) == 1
    sent = model.calls[0]
    assert sent["tool_choice"] == "required" and sent["parallel_tool_calls"] is False
    schema = convert_to_openai_tool(sent["tools"][0])["function"]["parameters"]
    from langchain_core.tools import StructuredTool
    assert isinstance(sent["tools"][0], StructuredTool)
    assert sent["tools"][0] is TOOL_DEFINITIONS["knowledge_search"].tool
    assert set(schema["properties"]) == {"query", "top_k", "filters"}
    assert set(schema["required"]) == {"query", "top_k"}
    payload = json.loads(sent["messages"][1][1])
    assert payload["objective"] == "查历史\n需要获取的信息：历史知识"
    assert "actor_id" not in payload


def test_csv_selection_uses_csv_request_schema():
    task = InvestigationTask(objective="统计申请数", expected_result="申请数", capability="COMPUTATION")
    source = InitialPlanningInput(user_query=UserQuery(query="统计", model_id="M"), available_capabilities=["COMPUTATION"])
    model = ToolModelStub(tool_response("csv_sample_statistics", {
        "dataset_id": "train", "statistic": "SAMPLE_COUNT",
        "window_start": "2025-09-01T00:00:00+08:00", "window_end": "2025-10-01T00:00:00+08:00",
    }))
    selected = InvestigationAgent(None, "test", tool_model=model).formulate_tool_call(
        task, source, goal="统计", tools=[d.tool for d in TOOL_DEFINITIONS.values() if d.capability == task.capability], tool_limits={})
    assert selected.tool_name == "csv_sample_statistics" and selected.arguments.statistic == "SAMPLE_COUNT"
    assert selected.arguments.field is None
    schema = convert_to_openai_tool(model.calls[0]["tools"][0])["function"]["parameters"]
    assert set(schema["properties"]) == {"dataset_id", "statistic", "window_start", "window_end", "field"}


@pytest.mark.parametrize("extra", ["actor_id", "execution_id", "collection", "session_factory"])
def test_tool_request_rejects_undeclared_fields(extra):
    with pytest.raises(ValidationError):
        choose(ToolModelStub(tool_response(arguments={"query": "q", "top_k": 1, extra: "forged"})))


def test_unknown_null_field_is_rejected_instead_of_silently_dropped():
    with pytest.raises(ValidationError):
        choose(ToolModelStub(tool_response(arguments={"query": "q", "top_k": 1, "dataset_id": None})))


@pytest.mark.parametrize("response", [
    AIMessage(content="我要调用工具", response_metadata={"finish_reason": "stop"}),
    tool_response(finish="length"), tool_response(name="unknown"),
    tool_response(arguments={"top_k": 1}),
    AIMessage(content="", response_metadata={"finish_reason": "tool_calls"}, tool_calls=tool_response().tool_calls * 2),
    AIMessage(content="", response_metadata={"finish_reason": "tool_calls"},
              invalid_tool_calls=[{"name": "knowledge_search", "args": "{", "id": "x", "error": "bad JSON"}]),
])
def test_invalid_response_never_falls_back(response):
    model = ToolModelStub(response)
    with pytest.raises(ValueError):
        choose(model)
    assert len(model.calls) == 1


def test_real_langchain_bind_and_response_parsing_without_network():
    requests = []
    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "chat-test", "object": "chat.completion", "created": 1,
            "model": "test", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                "function": {"name": "knowledge_search", "arguments": '{"query":"history","top_k":1}'}}]}}]})
    from openai import OpenAI, AsyncOpenAI
    import asyncio
    with httpx.Client(transport=httpx.MockTransport(serve)) as http:
        sdk = OpenAI(api_key="test", base_url="https://example.test/v1", http_client=http)
        async_sdk = AsyncOpenAI(api_key="test", base_url="https://example.test/v1")
        try:
            model = ChatOpenAI(model="test", api_key="test", client=sdk.chat.completions,
                               async_client=async_sdk.chat.completions)
            assert choose(model).arguments.query == "history"
        finally:
            asyncio.run(async_sdk.close())
            sdk.close()
    assert requests[0]["tools"][0]["function"]["name"] == "knowledge_search"
    assert requests[0]["thinking"] == {"type": "disabled"}
