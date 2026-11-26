"""压缩、只读回查与分页的业务边界；不使用真实模型。"""
import asyncio
import json
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from risk_platform.core.token_budget import request_tokens, project_read_messages, paragraph_page, ContextBudgetExceeded
from risk_platform.conversations.agent import ChatAgent
from risk_platform.conversations import agent as inputs
from risk_platform.workflow.planning import planning_messages
from test_personal_memory import SequenceModel, NOTE, INDEX, answer
from tool_model_helpers import tool_response

SOURCES = {"read_skill":("name", "resource", "digest"), "read_memory":("note_id", "version")}


def read_pair(identity, name, value):
    call = dict(role="assistant", tool_calls=[dict(id=identity, type="function",
        function=dict(name=name, arguments='{}'))])
    return [call, dict(role="tool", tool_call_id=identity, content=json.dumps(value))]


def test_projection_preserves_pairs_latest_body_and_original_records():
    old = dict(name="quality", resource="SKILL.md", digest="abc", content="old "*2000)
    recent = dict(note_id="N", version=1, content="RECENT_BODY")
    messages = [*read_pair("a", "read_skill", old), *read_pair("b", "read_memory", recent)]
    result = project_read_messages(messages, max_tokens=500, source_fields=SOURCES)
    assert json.loads(result[1]["content"])["body_omitted"] is True
    assert json.loads(result[1]["content"])["digest"] == "abc"
    assert result[0] == messages[0] and result[2:] == messages[2:]
    assert json.loads(messages[1]["content"])["content"] == old["content"]
    assert request_tokens(result) < request_tokens(messages)


@pytest.mark.parametrize("name", ["publish", "unknown", "read_memory"])
def test_projection_cannot_remove_unknown_or_unlocated_results(name):
    messages = read_pair("a", name, dict(content="x "*2000)) + read_pair("b", "read_skill", {})
    assert project_read_messages(messages, max_tokens=10, source_fields=SOURCES) == messages


def test_paragraph_pages_are_complete_and_oversized_paragraph_is_explicit():
    text = "first "*120+"\n\n"+"second "*120
    page = paragraph_page(text, start=0, max_tokens=150)
    assert page["content"] == "first "*120 and page["next_paragraph"] == 1
    assert paragraph_page(text, start=1, max_tokens=150)["next_paragraph"] is None
    with pytest.raises(ContextBudgetExceeded):
        paragraph_page(text, start=0, max_tokens=20)


def test_latest_parallel_read_batch_is_protected_as_one_protocol_group():
    first = read_pair("a", "read_skill", dict(name="one",resource="SKILL.md",digest="a",content="A "*1000))
    second = read_pair("b", "read_skill", dict(name="two",resource="SKILL.md",digest="b",content="B "*1000))
    call = dict(first[0], tool_calls=[*first[0]["tool_calls"], *second[0]["tool_calls"]])
    messages = [call, first[1], second[1]]
    assert project_read_messages(messages,max_tokens=10,source_fields=SOURCES) == messages


def test_multiple_old_results_can_be_projected_without_orphaning_calls():
    first = read_pair("a", "read_skill", dict(name="one",resource="SKILL.md",digest="a",content="A "*1000))
    second = read_pair("b", "read_skill", dict(name="two",resource="SKILL.md",digest="b",content="B "*1000))
    call = dict(first[0], tool_calls=[*first[0]["tool_calls"], *second[0]["tool_calls"]])
    messages = [call, first[1], second[1], *read_pair("c","read_memory",dict(note_id="N",version=1,content="latest"))]
    result = project_read_messages(messages,max_tokens=500,source_fields=SOURCES)
    assert all(json.loads(result[i]["content"])["body_omitted"] for i in (1,2))
    assert result[0] == call and result[-1] == messages[-1]


def test_planning_restore_uses_committed_projection_and_keeps_source():
    pair = read_pair("a", "read_skill", dict(name="quality",resource="SKILL.md",digest="v1",content="SOURCE"))
    events = [dict(kind="catalogue"), dict(kind="model",messages=pair[:1]), dict(kind="read",messages=pair[1:]),
              dict(kind="context",through=0,summary=None,omitted_calls=["a"])]
    restored = planning_messages(events)
    assert "SOURCE" not in restored[1]["content"]
    assert "SOURCE" in events[2]["messages"][0]["content"]
    assert restored[0]["tool_calls"][0]["id"] == restored[1]["tool_call_id"]


def test_note_read_is_saved_as_complete_pair_before_continuation():
    model = SequenceModel([tool_response("read_memory", {"note_id":"N"}), answer()])
    saved = []
    async def read(_): return NOTE
    async def save(pair):
        assert len(model.calls) == 1
        saved.append(pair)
    result = asyncio.run(ChatAgent("test",tool_model=model).decide("解释",history=[],memory_index=INDEX,
        read_memory=read,record_read=save))
    assert result.answer == "ok" and len(saved) == 1
    assert saved[0][0]["tool_calls"][0]["id"] == saved[0][1]["tool_call_id"]
    assert "PRIVATE_BODY" in saved[0][1]["content"]


def test_history_tool_reads_existing_turns_without_entry_action():
    model = SequenceModel([tool_response("read_chat_history", {"from_turn":1,"to_turn":2}), answer()])
    calls = []
    async def read(first,last,budget,interaction,start):
        calls.append((first,last,budget))
        return dict(turns=[dict(turn_number=1,user="不要发布",assistant="未发布")],next_turn=None)
    result = asyncio.run(ChatAgent("test",tool_model=model).decide("我说过什么？",history=[],memory_index=[],
        read_memory=None,read_history=read))
    assert result.answer == "ok" and calls[0][:2] == (1,2)
    assert any(isinstance(m,ToolMessage) and "不要发布" in m.content for m in model.calls[-1]["messages"])


def test_duplicate_range_reuses_loaded_note_and_new_call_id():
    first = tool_response("read_memory", {"note_id":"N"})
    second = first.model_copy(deep=True)
    second.tool_calls[0]["id"] = "second"
    model = SequenceModel([first, second, answer()])
    reads=[]
    async def read(identity): reads.append(identity); return NOTE
    asyncio.run(ChatAgent("test",tool_model=model).decide("解释",history=[],memory_index=INDEX,read_memory=read))
    assert reads == ["N"]


def test_new_note_version_is_reported_instead_of_claiming_old_body():
    model = SequenceModel([tool_response("read_memory", {"note_id":"N"}), answer()])
    async def read(_): return NOTE.model_copy(update={"version":2,"content":"NEW_BODY"})
    asyncio.run(ChatAgent("test",tool_model=model).decide("解释",history=[],memory_index=INDEX,read_memory=read))
    body = next(json.loads(m.content) for m in model.calls[-1]["messages"] if isinstance(m,ToolMessage))
    assert body["version"] == 2 and body["previous_version"] == 1 and body["content"] == "NEW_BODY"


def test_after_read_budget_rebuilds_old_history_before_next_model_call(monkeypatch):
    messages, tools = inputs.entry_request("解释",history=[],summary=None,memory_index=INDEX)
    cap = request_tokens(messages,tools)+1800
    monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",cap)
    model = SequenceModel([tool_response("read_memory", {"note_id":"N"}), answer()])
    reserves=[]
    async def read(_): return NOTE
    async def fit(reserve):
        reserves.append(reserve)
        return inputs.entry_request("解释",history=[],summary="旧问题已概括",memory_index=INDEX)[0]
    asyncio.run(ChatAgent("test",tool_model=model).decide("解释",
        history=[dict(role="user",content="old "*1200)],memory_index=INDEX,read_memory=read,fit_history=fit))
    assert reserves and len(model.calls)==2
