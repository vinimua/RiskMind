"""Configured local lifespan -> real DB/checkpointer/Runner; external providers stubbed."""
import asyncio
from types import SimpleNamespace as NS
import httpx
from test_ledger_postgres import database,pytestmark
from test_entrypoints_postgres import Planner
from test_entry_actions_postgres import Decider,proposal
from test_recovery_postgres import retrieval
from risk_platform import bootstrap
from risk_platform.workflow.checkpoints import postgres_checkpointer
from risk_platform.conversations.contracts import ChatDraft


def test_configured_http_lifecycle_and_investigation(database,monkeypatch):
    from risk_platform.knowledge import milvus
    agent=Planner();retrieval(monkeypatch,agent)
    decider=Decider(ChatDraft(answer="hello"));decider.client=NS(close=lambda:None)
    for key,value in {"RISK_LOCAL_MODE":"1","RISK_LOCAL_ACTOR_ID":"local-user",
        "RISK_LOCAL_API_TOKEN":"test-token-"+"a"*32,"RISK_ALLOWED_MODEL_IDS":'["M"]',
        "RISK_KNOWLEDGE_PROFILE":"parent-child-v1","SILICONFLOW_API_KEY":"test-embedding",
        "MILVUS_URI":"http://localhost:19530"}.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr(bootstrap,"create_llm_agents",lambda:(decider,agent))
    monkeypatch.setattr(milvus,"get_client",lambda uri:NS(close=lambda:None))
    async def scenario():
        async with postgres_checkpointer(database.render_as_string(hide_password=False),setup=True): pass
        app=bootstrap.create_local_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
                body={"request_id":"ordinary","message":"hello"}
                assert (await client.post("/chat/messages",json=body)).status_code==401
                client.headers["Authorization"]="Bearer "+"test-token-"+"a"*32
                answer=await client.post("/chat/messages",json=body)
                assert answer.status_code==200 and answer.json()["result"]["answer"]=="hello"
                assert (await client.post("/chat/messages",json=body)).json()==answer.json()
                assert len(decider.calls)==1
                decider.result=proposal()
                pending=(await client.post("/chat/messages",json={"request_id":"investigate","message":"Investigate M"})).json()
                assert pending["status"]=="PENDING_CONFIRMATION" and not agent.events
                response=await client.post("/chat/turns/"+pending["turn_id"]+"/confirmation",json={
                    "invocation_id":pending["invocation_id"],"decision_digest":pending["decision_digest"],"confirmed":True})
                assert response.status_code==200 and response.json()["status"]=="SUCCEEDED",response.text
                assert "initial-plan" in agent.events
        assert app.state.entry_services is None
    asyncio.run(scenario())
