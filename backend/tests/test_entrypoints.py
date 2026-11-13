from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from risk_platform.main import create_app
from risk_platform.conversations.contracts import ChatRequest, ChatResponse, EntryActionState, EntryConfirmation
from risk_platform.entrypoints import AlertRequest

ALERT = dict(alert_id="AL1",model_id="M",metric="PSI",symptom="drift",window_start="2026-11-09T00:00:00Z",window_end="2026-11-10T00:00:00Z",
             current_value=0.3,reference_value=0.1,triggered_at="2026-11-10T00:00:00Z")

class Services:
    def __init__(self): self.calls=[]
    async def from_alert(self, request, *, actor):
        self.calls.append((actor,request.alert_id))
        return {"investigation_id":"I"}
    async def chat(self, request, *, actor):
        self.calls.append((actor,"ENTRY_DECISION"))
        return EntryActionState(conversation_id="C",turn_id="T",status="ANSWERED",result=ChatResponse(conversation_id="C",turn_id="T",status="SUCCEEDED",answer="ok"))

def test_routes_require_server_auth_and_pass_authenticated_actor():
    assert TestClient(create_app()).post("/chat/messages",json={"request_id":"Q","message":"hi"}).status_code==503
    service=Services()
    client=TestClient(create_app(entry_services=service,authenticate=lambda request:"server-actor"))
    assert client.post("/investigations/from-alert",json=ALERT,headers={"X-Actor-ID":"spoof"}).status_code==200
    assert client.post("/chat/messages",json={"request_id":"Q","message":"hi"}).status_code==200
    assert service.calls==[("server-actor","AL1"),("server-actor","ENTRY_DECISION")]
    assert client.post("/chat/messages",json={"request_id":"Q","message":"hi","actor_id":"spoof"}).status_code==422

@pytest.mark.parametrize("body",[
    {"request_id":"Q","message":"hi","action":"READ_INVESTIGATION"},
    {"request_id":"Q","message":"hi","action":"START_INVESTIGATION"},
    {"request_id":"Q","message":"hi","investigation_id":"I","model_id":"M"},
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


def test_alert_api_rejects_old_or_mixed_window_fields():
    client=TestClient(create_app(entry_services=Services(),authenticate=lambda _:"A"))
    old={key:value for key,value in ALERT.items() if key not in ("window_start","window_end")}
    assert client.post("/investigations/from-alert",json={**old,"time_window":"today"}).status_code==422
    assert client.post("/investigations/from-alert",json={**ALERT,"time_window":"today"}).status_code==422


def test_chat_confirmation_and_inspection_routes():
    service=Services()
    calls=[]
    async def confirm(turn_id, body, *, actor):
        calls.append((turn_id,body.confirmed,actor))
        return EntryActionState(conversation_id="C",turn_id=turn_id,status="STARTED")
    async def inspect(turn_id, *, actor):
        calls.append((turn_id,actor))
        return EntryActionState(conversation_id="C",turn_id=turn_id,status="PENDING_CONFIRMATION")
    service.confirm_chat=confirm;service.inspect_entry=inspect
    client=TestClient(create_app(entry_services=service,authenticate=lambda _:"A"))
    body={"invocation_id":"call","decision_digest":"a"*64,"confirmed":True}
    assert client.post("/chat/turns/T/confirmation",json=body).status_code==202
    assert client.get("/chat/turns/T").status_code==202
    assert calls==[("T",True,"A"),("T","A")]
    assert client.post("/chat/turns/T/confirmation",json={**body,"actor":"B"}).status_code==422
    assert client.post("/chat/messages",json={"request_id":"Q","message":"hi","action":"AUTO"}).status_code==422
