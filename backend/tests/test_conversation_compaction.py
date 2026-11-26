"""会话请求预算及摘要模型契约；不使用真实供应商。"""
import asyncio
import json
import pytest
from langchain_core.messages import AIMessage
from risk_platform.conversations import agent as inputs
from risk_platform.conversations.agent import ChatAgent
from risk_platform.core.token_budget import ContextBudgetExceeded
from tool_model_helpers import ToolModelStub, tool_response


def test_budget_counts_system_history_summary_index_and_tool_declarations():
    messages, tools = inputs.entry_request("问题",history=[],summary=None,memory_index=[])
    baseline = inputs.request_tokens(messages, tools)
    for field in ("history", "summary", "memory_index"):
        values=dict(history=[],summary=None,memory_index=[])
        values[field] = ([{"role":"user","content":"原文 "*500}] if field=="history" else
            "摘要 "*500 if field=="summary" else [{"note_id":"N","version":1,"description":"说明 "*500}])
        request, declarations=inputs.entry_request("问题",**values)
        assert inputs.request_tokens(request,declarations)>baseline
    assert inputs.request_tokens(messages,tools)>inputs.request_tokens(messages)


def test_hard_budget_prevents_call_and_reserves_output(monkeypatch):
    monkeypatch.setattr(inputs,"CHAT_CONTEXT_WINDOW",6000)
    assert inputs.available_input_tokens(4096)==880
    model=ToolModelStub(AIMessage(content='{"answer":"ok"}',response_metadata={"finish_reason":"stop"}))
    with pytest.raises(ContextBudgetExceeded):
        asyncio.run(ChatAgent("test",tool_model=model).decide("长消息 "*3000,
            history=[],memory_index=[],read_memory=None))
    assert not model.calls


@pytest.mark.parametrize("reply",[
    AIMessage(content='{"summary":"内容"}',response_metadata={"finish_reason":"length"}),
    AIMessage(content='{"summary":" "}',response_metadata={"finish_reason":"stop"}),
    tool_response("read_memory",{"note_id":"N"}),
])
def test_incomplete_summary_is_not_accepted(reply):
    model=ToolModelStub(reply)
    with pytest.raises(ValueError):
        asyncio.run(ChatAgent("test",tool_model=model).summarize("旧摘要",[
            dict(turn_id="T",turn_number=1,user="原问题",assistant="原回答")]))
    assert len(model.calls)==1


def test_summary_is_separate_from_personal_memory_and_has_no_tools():
    model=ToolModelStub(AIMessage(content='{"summary":"目标未完成，仍需核对来源"}',response_metadata={"finish_reason":"stop"}))
    result=asyncio.run(ChatAgent("test",tool_model=model).summarize("旧摘要",[
        dict(turn_id="T",turn_number=1,user="不要认定成功",assistant="尚未确认")]))
    assert "未完成" in result
    call=model.calls[0]
    assert "tools" not in call and call["max_tokens"]==inputs.SUMMARY_OUTPUT_TOKENS
    assert json.loads(call["messages"][1].content)["previous_summary"]=="旧摘要"
