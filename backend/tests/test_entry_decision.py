"""阶段 1：模型输出只产生候选，不调用任何案件服务或付费模型。"""
import json
from types import SimpleNamespace as NS
import pytest
from pydantic import ValidationError
from risk_platform.conversations.agent import ChatAgent, ENTRY_DECISION_PROMPT
from risk_platform.conversations.contracts import (
    ChatDraft, ToolInvocation, ProgressReadArguments, StartInvestigationArguments,
)
from risk_platform.conversations.tools import ENTRY_TOOL_NAMES, entry_tool_declarations, parse_entry_decision
from risk_platform.memory.contracts import AgentContext, MemoryScope


def tool_call(name="get_investigation_progress", arguments='{"investigation_id":"case-001"}', call_id="call-1", kind="function"):
    return NS(id=call_id,type=kind,function=NS(name=name,arguments=arguments))


def response(content=None, calls=None, finish="stop", refusal=None):
    return NS(choices=[NS(finish_reason=finish,message=NS(content=content,tool_calls=calls,refusal=refusal))])


class Client:
    def __init__(self, value):
        self.value=value;self.calls=[]
        self.chat=NS(completions=NS(create=self.create))
    def create(self,**kwargs):
        self.calls.append(kwargs)
        if isinstance(self.value,Exception): raise self.value
        return self.value


def decide(client, message="查询案件 case-001 的进度", **kwargs):
    return ChatAgent(client,"test-model").decide(message,history=[],
        memory_context=AgentContext(scope=MemoryScope(kind="CONVERSATION",id="C")),**kwargs)


def test_one_call_returns_answer_and_memory_without_tools_execution():
    client=Client(response('{"answer":"PSI 用于比较分布。","memory":{"user_preferences":["使用中文"]}}'))
    result=decide(client,"PSI 是什么？")
    assert isinstance(result,ChatDraft) and result.memory.user_preferences==("使用中文",)
    assert len(client.calls)==1
    request=client.calls[0]
    assert request["tool_choice"]=="auto" and request["parallel_tool_calls"] is False
    assert len(request["tools"])==2
    assert request["response_format"]=={"type":"json_object"}


@pytest.mark.parametrize("name,arguments,expected",[
    ("get_investigation_progress",'{"investigation_id":"case-001"}',ProgressReadArguments),
    ("start_investigation",'{"model_id":"credit-v3"}',StartInvestigationArguments),
])
def test_one_native_call_returns_typed_proposal(name,arguments,expected):
    client=Client(response(calls=[tool_call(name,arguments)],finish="tool_calls"))
    result=decide(client)
    assert isinstance(result,ToolInvocation) and result.call_id=="call-1"
    assert result.name==name and isinstance(result.arguments,expected)
    assert len(client.calls)==1
    assert ToolInvocation.model_validate_json(result.model_dump_json())==result


def test_standard_endpoint_declarations_keep_local_schema_and_no_trusted_fields():
    for declaration in entry_tool_declarations():
        tool=declaration["function"];schema=tool["parameters"]
        assert tool["strict"] is False and schema["additionalProperties"] is False
        assert set(schema["required"])==set(schema["properties"])
        assert len(schema["properties"])==1
        assert not {"actor_id","confirmed","investigation_status"}.intersection(schema["properties"])


def test_offered_subset_and_empty_tools_are_respected():
    client=Client(response('{"answer":"请明确查询目标。","memory":{}}'))
    decide(client,tool_names=())
    assert "tools" not in client.calls[0] and "tool_choice" not in client.calls[0]
    client=Client(response(calls=[tool_call("start_investigation",'{"model_id":"M"}')],finish="tool_calls"))
    with pytest.raises(ValueError): decide(client,tool_names=("get_investigation_progress",))
    assert len(client.calls)==1


@pytest.mark.parametrize("names",[("unknown",),("start_investigation","start_investigation")])
def test_invalid_offering_fails_before_model_call(names):
    client=Client(response())
    with pytest.raises(ValueError): decide(client,tool_names=names)
    assert not client.calls


@pytest.mark.parametrize("value",[
    response(calls=[tool_call(),tool_call()],finish="tool_calls"),
    response(content="我先查询。",calls=[tool_call()],finish="tool_calls"),
    response(content="{}",calls=[tool_call()],finish="tool_calls"),
    response(calls=[tool_call()],finish="length"),
    response(calls=[tool_call()],finish="stop"),
    response(calls=[tool_call(kind="custom")],finish="tool_calls"),
    response(calls=[tool_call(name="delete_case")],finish="tool_calls"),
    response(calls=[tool_call(call_id="")],finish="tool_calls"),
    response(calls=[tool_call(arguments='{"investigation_id":')],finish="tool_calls"),
    response(calls=[tool_call(arguments='{}')],finish="tool_calls"),
    response(calls=[tool_call(arguments='{"investigation_id":"I","actor_id":"admin"}')],finish="tool_calls"),
    response(calls=[tool_call("start_investigation",'{"model_id":"M","confirmed":true}')],finish="tool_calls"),
    response(calls=[tool_call(arguments='{"investigation_id":"A","investigation_id":"B"}')],finish="tool_calls"),
    response(calls=[tool_call(arguments='{"investigation_id":NaN}')],finish="tool_calls"),
    response(calls=[tool_call(arguments='[]')],finish="tool_calls"),
    response(calls=[tool_call(arguments='{"model_id":"M"}')],finish="tool_calls"),
    response(content='{"answer":"a","memory":{}}',finish="length"),
    response(content='{"answer":"a","memory":{}}',finish="tool_calls"),
    response(content='{"answer":"a","memory":{}}',refusal="refused"),
    response(content='{"answer":"a","answer":"b"}'),
    response(content='{"answer":"a","memory":{"execution_summaries":["调查成功"]}}'),
    response(content='{"answer":"a","status":"SUCCEEDED"}'),
    response(content='{"name":"start_investigation","arguments":{"model_id":"M"}}'),
    response(content='{"answer":""}'), response(content="not json"),response(),
    NS(choices=[]), NS(choices=[NS(),NS()]),
])
def test_invalid_output_is_rejected_without_retry_or_fallback(value):
    client=Client(value)
    with pytest.raises(ValueError): decide(client)
    assert len(client.calls)==1


def test_provider_failure_propagates_without_second_call():
    error=RuntimeError("provider failed");client=Client(error)
    with pytest.raises(RuntimeError) as caught: decide(client)
    assert caught.value is error and len(client.calls)==1


def test_tool_identity_and_arguments_cannot_be_crossed():
    with pytest.raises(ValidationError):
        ToolInvocation(call_id="x",name="get_investigation_progress",arguments=StartInvestigationArguments(model_id="M"))


def test_real_sdk_response_shape_is_parsed_without_network():
    from openai.types.chat import ChatCompletion
    value=ChatCompletion.model_validate({"id":"completion-1","created":1,"model":"test-model","object":"chat.completion",
        "choices":[{"index":0,"finish_reason":"tool_calls","message":{"role":"assistant","content":None,
        "tool_calls":[{"id":"call-1","type":"function","function":{"name":"start_investigation","arguments":'{"model_id":"M"}'}}]}}]})
    result=decide(Client(value))
    assert result.name=="start_investigation" and result.arguments.model_id=="M"


def test_ambiguous_request_can_return_question_in_same_call():
    client=Client(response('{"answer":"你希望查询哪个案件，还是启动新调查？","memory":{}}'))
    assert "？" in decide(client,"帮我看看那个").answer
    assert len(client.calls)==1
    # 只证明约束传给模型，不把模拟输出测试称为真实语义评测。
    assert "否定" in ENTRY_DECISION_PROMPT and "不能声称" in ENTRY_DECISION_PROMPT
    assert "显式确认" in ENTRY_DECISION_PROMPT


def test_explicit_target_and_history_are_data_not_system_instructions():
    client=Client(response('{"answer":"请确认对象。","memory":{}}'))
    decide(client,investigation_id="case-001")
    request=client.calls[0]
    data=json.loads(request["messages"][1]["content"])
    assert data["explicit_target"]=={"investigation_id":"case-001","model_id":None}
    assert request["messages"][0]["role"]=="system" and request["messages"][1]["role"]=="user"


def test_decision_observability_records_tool_name_without_body_or_target(monkeypatch):
    from contextlib import contextmanager
    from risk_platform.core import observability as obs
    records=[]
    class Span:
        def update(self,**kwargs): records.append(kwargs)
    class Observer:
        @contextmanager
        def start_as_current_observation(self,**kwargs):
            records.append(kwargs)
            yield Span()
    monkeypatch.setattr(obs,"get_langfuse",lambda:Observer())
    value=response(calls=[tool_call(arguments='{"investigation_id":"PRIVATE_TARGET"}')],finish="tool_calls")
    value.usage=NS(prompt_tokens=3,completion_tokens=2,total_tokens=5)
    decide(Client(value),"PRIVATE_BODY")
    assert records[0]["name"]=="entry-decision"
    assert records[-1]["metadata"]=={"selected_tool":"get_investigation_progress"}
    assert "PRIVATE_TARGET" not in str(records) and "PRIVATE_BODY" not in str(records)
    assert any(record.get("usage_details")=={"input":3,"output":2,"total":5} for record in records)
