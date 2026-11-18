"""入口模型只接受 LangChain AIMessage；不执行实际案件操作。"""
import json
import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from risk_platform.conversations.agent import ChatAgent, ENTRY_DECISION_PROMPT
from risk_platform.conversations.contracts import ChatDraft, ToolInvocation, StartInvestigationArguments
from risk_platform.conversations.tools import entry_tool_declarations
from risk_platform.memory.contracts import AgentContext, MemoryScope
from tool_model_helpers import ToolModelStub, tool_response


def answer(content='{"answer":"回答","memory":{}}', finish="stop"):
    return AIMessage(content=content, response_metadata={"finish_reason": finish})


def decide(model, **kwargs):
    return ChatAgent("test", tool_model=model).decide("查询案件 case-001", history=[],
        memory_context=AgentContext(scope=MemoryScope(kind="CONVERSATION", id="C")), **kwargs)


def test_one_decision_binds_each_entry_tools_own_schema():
    model = ToolModelStub(answer())
    assert isinstance(decide(model), ChatDraft)
    assert len(model.calls) == 1
    sent = model.calls[0]
    assert sent["tool_choice"] == "auto" and sent["parallel_tool_calls"] is False
    assert sent["response_format"] == {"type": "json_object"}
    assert len(sent["tools"]) == 2
    schemas = {d["function"]["name"]: d["function"]["parameters"] for d in sent["tools"]}
    for name, field in (("get_investigation_progress", "investigation_id"), ("start_investigation", "model_id")):
        assert set(schemas[name]["properties"]) == {field}
        assert schemas[name]["required"] == [field]
        assert schemas[name]["additionalProperties"] is False


@pytest.mark.parametrize("name,args", [
    ("start_investigation", {"model_id": "M"}),
    ("get_investigation_progress", {"investigation_id": "case-001"}),
])
def test_native_call_is_only_a_typed_proposal(name, args):
    model = ToolModelStub(tool_response(name, args))
    value = decide(model)
    assert isinstance(value, ToolInvocation) and value.name == name and value.call_id == "call-test"
    assert value.arguments.model_dump() == args
    assert len(model.calls) == 1


def test_empty_tools_does_not_bind_or_offer_default_tools():
    model = ToolModelStub(answer())
    decide(model, tool_names=())
    assert "tools" not in model.calls[0]


def test_explicit_target_is_user_data():
    model = ToolModelStub(answer())
    decide(model, investigation_id="case-001")
    messages = model.calls[0]["messages"]
    assert messages[0][0] == "system" and messages[1][0] == "human"
    assert json.loads(messages[1][1])["explicit_target"] == {"investigation_id": "case-001", "model_id": None}
    assert "显式确认" in ENTRY_DECISION_PROMPT


@pytest.mark.parametrize("names", [("unknown",), ("start_investigation", "start_investigation")])
def test_invalid_offering_fails_before_model(names):
    model = ToolModelStub(answer())
    with pytest.raises(ValueError):
        decide(model, tool_names=names)
    assert not model.calls


@pytest.mark.parametrize("response", [
    answer(finish="length"), answer("not json"), answer("{}"),
    answer('{"answer":"a","answer":"b"}'),
    answer('{"answer":"a","memory":{"execution_summaries":["伪造执行"]}}'),
    tool_response("unknown", {}),
    tool_response("start_investigation", {}),
    tool_response("start_investigation", {"model_id": "M", "confirmed": True}),
    tool_response("start_investigation", {"model_id": "M", "query": "unrelated"}),
    tool_response("start_investigation", {"model_id": "M", "query": None}),
    tool_response("start_investigation", {"model_id": "M"}, finish="length"),
    AIMessage(content="正文", tool_calls=tool_response("start_investigation", {"model_id": "M"}).tool_calls,
              response_metadata={"finish_reason": "tool_calls"}),
    AIMessage(content="", invalid_tool_calls=[{"name": "start_investigation", "args": "{", "id": "x", "error": "bad JSON"}],
              response_metadata={"finish_reason": "tool_calls"}),
    AIMessage(content="", tool_calls=tool_response().tool_calls * 2, response_metadata={"finish_reason": "tool_calls"}),
])
def test_invalid_outputs_have_no_retry_or_fallback(response):
    model = ToolModelStub(response)
    with pytest.raises(ValueError):
        decide(model)
    assert len(model.calls) == 1


def test_unoffered_tool_is_rejected():
    with pytest.raises(ValueError):
        decide(ToolModelStub(tool_response("start_investigation", {"model_id": "M"})),
               tool_names=("get_investigation_progress",))


def test_tool_identity_and_arguments_cannot_be_crossed():
    with pytest.raises(ValidationError):
        ToolInvocation(call_id="x", name="get_investigation_progress", arguments=StartInvestigationArguments(model_id="M"))


def test_provider_error_propagates_once():
    model = ToolModelStub(RuntimeError("provider failed"))
    with pytest.raises(RuntimeError, match="provider failed"):
        decide(model)
    assert len(model.calls) == 1
