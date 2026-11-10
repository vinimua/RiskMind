from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from risk_platform.main import create_app
from risk_platform.conversations.contracts import ChatRequest, ChatResponse
from risk_platform.entrypoints import AlertRequest

ALERT = dict(alert_id="AL1",model_id="M",metric="PSI",symptom="drift",time_window="today",
             current_value=0.3,reference_value=0.1,triggered_at="2026-11-10T00:00:00Z")

class Services:
    def __init__(self): self.calls=[]
    async def from_alert(self, request, *, actor):
        self.calls.append((actor,request.alert_id))
        return {"investigation_id":"I"}
    async def chat(self, request, *, actor):
        self.calls.append((actor,request.action))
        return ChatResponse(conversation_id="C",turn_id="T",status="SUCCEEDED",answer="ok")

def test_routes_require_server_auth_and_pass_authenticated_actor():
    assert TestClient(create_app()).post("/chat/messages",json={"request_id":"Q","message":"hi"}).status_code==503
    service=Services()
    client=TestClient(create_app(entry_services=service,authenticate=lambda request:"server-actor"))
    assert client.post("/investigations/from-alert",json=ALERT,headers={"X-Actor-ID":"spoof"}).status_code==200
    assert client.post("/chat/messages",json={"request_id":"Q","message":"hi"}).status_code==200
    assert service.calls==[("server-actor","AL1"),("server-actor","CHAT")]
    assert client.post("/chat/messages",json={"request_id":"Q","message":"hi","actor_id":"spoof"}).status_code==422

@pytest.mark.parametrize("body",[
    {"request_id":"Q","message":"hi","action":"READ_INVESTIGATION"},
    {"request_id":"Q","message":"hi","action":"START_INVESTIGATION"},
    {"request_id":"Q","message":"hi","investigation_id":"I"},
])
def test_invalid_actions_rejected(body):
    with pytest.raises(ValidationError): ChatRequest(**body)

def test_failure_is_sanitized_and_alert_contract_is_strict():
    async def fail(*args,**kwargs): raise PermissionError("private credentials")
    service=Services();service.from_alert=fail
    client=TestClient(create_app(entry_services=service,authenticate=lambda _:"A"))
    response=client.post("/investigations/from-alert",json=ALERT)
    assert response.status_code==403 and "private" not in response.text
    with pytest.raises(ValidationError): AlertRequest(**dict(ALERT,current_value=float("nan")))
    with pytest.raises(ValidationError): AlertRequest(**dict(ALERT,triggered_at="2026-11-10T00:00:00"))


def test_chat_agent_validates_model_output_and_never_exposes_tools():
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.memory.contracts import AgentContext,MemoryScope
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(
            content='{"answer":"ok","memory":{"user_preferences":["Chinese"]}}'))])
    agent=ChatAgent(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),"test-model")
    result=agent.reply("hi",history=[],memory_context=AgentContext(scope=MemoryScope(kind="CONVERSATION",id="C")))
    assert result.memory.user_preferences==("Chinese",) and "tools" not in calls[0]
    def forged(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",message=SimpleNamespace(
            content='{"answer":"ok","memory":{"execution_summaries":["trained"]}}'))])
    agent.client.chat.completions.create=forged
    with pytest.raises(ValueError):
        agent.reply("hi",history=[],memory_context=AgentContext(scope=MemoryScope(kind="CONVERSATION",id="C")))
