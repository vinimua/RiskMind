"""观测降级、数据边界与异步上下文传播；不向远程平台发送请求。"""
import asyncio
import sys
from contextlib import contextmanager
from types import SimpleNamespace, ModuleType
import pytest
from risk_platform.core import observability as obs
from risk_platform.conversations.agent import ChatAgent
from risk_platform.conversations.contracts import ChatResponse
from risk_platform.memory.contracts import AgentContext, MemoryScope

class Span:
    def __init__(self, values): self.values=values; self.updates=[]
    def update(self,**kwargs): self.updates.append(kwargs)

class Client:
    def __init__(self, failure=None): self.spans=[];self.exits=[];self.events=[];self.failure=failure
    def start_as_current_observation(self,**kwargs):
        if self.failure=="start": raise RuntimeError("secret SDK error")
        span=Span(kwargs); self.spans.append(span)
        owner=self
        class Manager:
            def __enter__(self):
                if owner.failure=="enter": raise RuntimeError("secret SDK error")
                if owner.failure=="update": span.update=lambda **_:(_ for _ in ()).throw(RuntimeError("secret SDK error"))
                return span
            def __exit__(self,*args):
                owner.exits.append(args)
                if owner.failure=="exit": raise RuntimeError("secret SDK error")
        return Manager()
    def create_event(self,**kwargs): self.events.append(kwargs)
    def shutdown(self): self.closed=True


def test_disabled_does_not_import_sdk(monkeypatch):
    monkeypatch.setenv("RISK_LANGFUSE_ENABLED","false")
    monkeypatch.setitem(sys.modules,"langfuse",None)
    with obs.observation("test") as span: assert span is None

@pytest.mark.parametrize("failure",["start","enter","update","exit"])
def test_sdk_failure_does_not_fail_or_repeat_business(monkeypatch,caplog,failure):
    client=Client(failure);monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    calls=[]
    @obs.observe_generation("chat-answer")
    def business(self): calls.append(1);obs.record_usage(SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2)));return "ok"
    assert business(SimpleNamespace(model="test"))=="ok" and calls==[1]
    assert "secret SDK error" not in caplog.text


def test_business_exception_preserved_without_exporting_raw_exception(monkeypatch):
    client=Client();monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    error=ValueError("private-body-and-token")
    with pytest.raises(ValueError) as caught:
        with obs.observation("test"): raise error
    assert caught.value is error
    assert client.exits==[(None,None,None)]
    assert "private-body-and-token" not in str(client.spans[0].updates)


def test_chat_model_instrumentation_only_exports_model_usage_and_intent(monkeypatch):
    client=Client();monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    from langchain_core.messages import AIMessage
    from tool_model_helpers import ToolModelStub
    message = AIMessage(content='{"answer":"private answer","memory_candidate":null}',
        response_metadata={"finish_reason":"stop"}, usage_metadata={"input_tokens":10,"output_tokens":4,"total_tokens":14})
    agent=ChatAgent("glm-test",tool_model=ToolModelStub(message))
    assert asyncio.run(agent.decide("private message",history=[],memory_index=[],read_memory=None)).answer=="private answer"
    assert [span.values["name"] for span in client.spans]==["entry-decision"]
    assert all(span.updates[0]["usage_details"]=={"input":10,"output":4,"total":14} for span in client.spans)
    assert "private" not in str([(span.values,span.updates) for span in client.spans])


def test_async_to_thread_shares_root_and_cancellation_is_not_swallowed(monkeypatch):
    client=Client();monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    async def scenario():
        with obs.observation("root") as span:
            assert await asyncio.to_thread(obs._current.get) is span
            with pytest.raises(asyncio.CancelledError):
                with obs.observation("cancel"): raise asyncio.CancelledError()
            assert obs._current.get() is span
        assert obs._current.get() is None
    asyncio.run(scenario())


def test_trace_marks_failure_and_cached_turn_without_message(monkeypatch):
    client=Client();monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    @obs.trace_chat
    async def business():
        obs.tag_chat_turn("chat-hash","turn-hash","AUTO",cached=True)
        return ChatResponse(conversation_id="chat-hash",turn_id="turn-hash",status="FAILED",answer="private error",error_code="DEPENDENCY")
    result=asyncio.run(business())
    assert result.status=="FAILED"
    assert client.spans[0].updates[0]["metadata"]["cached"] is True
    assert client.spans[0].updates[-1]["level"]=="ERROR"
    assert "private" not in str(client.spans[0].updates)


def test_missing_configuration_and_sdk_fail_closed_to_noop(monkeypatch,caplog):
    monkeypatch.setenv("RISK_LANGFUSE_ENABLED","true")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY",raising=False)
    assert obs.get_langfuse() is None
    for key in ("LANGFUSE_PUBLIC_KEY","LANGFUSE_SECRET_KEY","LANGFUSE_BASE_URL"):
        monkeypatch.setenv(key,"private-setting")
    monkeypatch.setattr(obs,"_client",None)
    monkeypatch.setitem(sys.modules,"langfuse",None)
    assert obs.get_langfuse() is None
    assert "private-setting" not in caplog.text


def test_lifespan_closes_observability_client(monkeypatch):
    from fastapi.testclient import TestClient
    from risk_platform.main import create_app
    client=Client();monkeypatch.setattr(obs,"_client",client)
    with TestClient(create_app()) as api:
        assert api.get("/health/live").status_code==200
    assert client.closed and obs._client is None


def test_configured_client_is_lazy_and_reused(monkeypatch):
    monkeypatch.setenv("RISK_LANGFUSE_ENABLED","true")
    for key in ("LANGFUSE_PUBLIC_KEY","LANGFUSE_SECRET_KEY","LANGFUSE_BASE_URL"):
        monkeypatch.setenv(key,"configured-value")
    created=[]
    def factory(**kwargs): created.append(kwargs);return Client()
    monkeypatch.setitem(sys.modules,"langfuse",SimpleNamespace(Langfuse=factory))
    monkeypatch.setattr(obs,"_client",None)
    assert obs.get_langfuse() is obs.get_langfuse()
    assert len(created)==1 and created[0]["timeout"]==2




def test_trace_handles_nested_entry_response(monkeypatch):
    from risk_platform.conversations.contracts import EntryActionState
    client=Client();monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    @obs.trace_chat
    async def business():
        return EntryActionState(conversation_id="C",turn_id="T",status="UNCERTAIN",
            result=ChatResponse(conversation_id="C",turn_id="T",status="FAILED",answer="private",error_code="DEPENDENCY"))
    asyncio.run(business())
    update=client.spans[0].updates[-1]
    assert update["level"]=="ERROR" and update["metadata"]["error_code"]=="DEPENDENCY"
    assert "private" not in str(update)
