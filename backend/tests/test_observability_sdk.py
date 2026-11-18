"""可选真实 SDK 离线测试：全部导出到内存，不需要 Langfuse 服务或真实密钥。"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4
import pytest


def test_real_sdk_context_usage_and_privacy(monkeypatch):
    langfuse=pytest.importorskip("langfuse")
    import httpx
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from risk_platform.core import observability as obs
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.conversations.contracts import ChatResponse
    from risk_platform.memory.contracts import AgentContext, MemoryScope
    exporter=InMemorySpanExporter()
    client=langfuse.Langfuse(public_key="pk-offline-"+uuid4().hex,secret_key="sk-offline",base_url="http://offline.invalid",
        span_exporter=exporter,httpx_client=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={})) ))
    monkeypatch.setattr(obs,"get_langfuse",lambda:client)
    from langchain_core.messages import AIMessage
    from tool_model_helpers import ToolModelStub
    message = AIMessage(content='{"answer":"PRIVATE_SENTINEL","memory":{}}',
        response_metadata={"finish_reason":"stop"},usage_metadata={"input_tokens":3,"output_tokens":2,"total_tokens":5})
    agent=ChatAgent("test-model",tool_model=ToolModelStub(message))
    @obs.trace_chat
    async def scenario():
        await asyncio.to_thread(agent.decide,"PRIVATE_SENTINEL",history=[],
            memory_context=AgentContext(scope=MemoryScope(kind="CONVERSATION",id="C")))
        with pytest.raises(ValueError):
            with obs.observation("failing-operation"): raise ValueError("PRIVATE_SENTINEL")
        return ChatResponse(conversation_id="C",turn_id="T",status="SUCCEEDED",answer="PRIVATE_SENTINEL")
    try:
        asyncio.run(scenario())
        client.flush()
        spans=exporter.get_finished_spans()
        assert {s.name for s in spans}>={"chat-request","entry-decision","failing-operation"}
        assert len({s.context.trace_id for s in spans})==1
        root=next(s for s in spans if s.name=="chat-request")
        assert all(s.parent.span_id==root.context.span_id for s in spans if s.name in ("entry-decision",))
        assert "PRIVATE_SENTINEL" not in str([(dict(s.attributes),s.events) for s in spans])
        assert any("usage" in key for span in spans for key in span.attributes)
    finally:
        client.shutdown()
