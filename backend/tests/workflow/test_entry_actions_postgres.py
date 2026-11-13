"""阶段 2：真实 PG、确认审计、并发领取和原 Runner；不调用真实模型。"""
import asyncio
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import resources, retrieval, seed
from test_entrypoints_postgres import Planner, service
from risk_platform.conversations.contracts import ChatRequest, ChatDraft, ToolInvocation, EntryConfirmation
from risk_platform.conversations.models import ChatTurnRow, EntryDecisionRow
from risk_platform.conversations.repository import ChatConflictError
from risk_platform.memory.contracts import MemoryContent
from risk_platform.workflow.persistence.models import InvestigationRow

class Decider:
    def __init__(self,result): self.result=result;self.calls=[]
    def decide(self,message,**kwargs):
        self.calls.append((message,kwargs))
        if isinstance(self.result,BaseException): raise self.result
        return self.result


def proposal():
    return ToolInvocation(call_id="untrusted-model-call",name="start_investigation",arguments={"model_id":"M"})


def confirmation(view,confirmed=True):
    return EntryConfirmation(invocation_id=view.invocation_id,decision_digest=view.decision_digest,confirmed=confirmed)


async def count_cases(sessions):
    async with sessions() as session: return len(list(await session.scalars(select(InvestigationRow))))


def test_start_requires_confirmation_then_reuses_one_real_runner(database,monkeypatch):
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,_,runner):
            decider=Decider(proposal());app=service(sessions,runner,decider)
            request=ChatRequest(request_id="start-new",message="Investigate M")
            pending=await app.prepare_entry(request,actor="A")
            assert pending.status=="PENDING_CONFIRMATION" and pending.invocation_id!="untrusted-model-call"
            assert not agent.events and await count_cases(sessions)==0
            assert await app.prepare_entry(request,actor="A")==pending and len(decider.calls)==1
            with pytest.raises(ChatConflictError): await app.execute_entry(pending.turn_id,actor="A")
            with pytest.raises(PermissionError): await app.confirm_entry(pending.turn_id,confirmation(pending),actor="B")
            with pytest.raises(ChatConflictError):
                await app.confirm_entry(pending.turn_id,confirmation(pending).model_copy(update={"decision_digest":"a"*64}),actor="A")
            ready=await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
            assert ready.status=="READY" and await count_cases(sessions)==0
            result=await app.execute_entry(pending.turn_id,actor="A")
            assert result.status=="SUCCEEDED" and result.result.status=="SUCCEEDED"
            assert result.investigation_id==pending.investigation_id and await count_cases(sessions)==1
            assert agent.events==["initial-plan","query:T0","search:T0","query:T1","search:T1","observation"]
            events=list(agent.events)
            assert await app.execute_entry(pending.turn_id,actor="A")==result
            assert await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")==result
            assert await app.prepare_entry(request,actor="A")==result
            assert agent.events==events and len(decider.calls)==1
            with pytest.raises(ChatConflictError):
                await app.prepare_entry(request.model_copy(update={"message":"different"}),actor="A")
            for statement in ["UPDATE chat_entry_decisions SET decision_payload='{}'", "DELETE FROM chat_entry_decisions",
                              "UPDATE chat_turns SET response_payload='{}'"]:
                with pytest.raises(DBAPIError):
                    async with sessions.begin() as session: await session.execute(text(statement))
    asyncio.run(scenario())


def test_read_only_action_answer_memory_and_rejection(database):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            await seed(sessions)
            decider=Decider(ToolInvocation(call_id="r",name="get_investigation_progress",arguments={"investigation_id":"I"}))
            app=service(sessions,runner,decider)
            app.authorize_resource=lambda actor,action,resource:action=="investigation:read"
            view=await app.prepare_entry(ChatRequest(request_id="read",message="案件 I 的进度"),actor="A")
            assert view.status=="READY"
            result=await app.execute_entry(view.turn_id,actor="A")
            assert result.status=="SUCCEEDED" and result.result.investigation_id=="I"
            with pytest.raises(PermissionError): await app.inspect_entry(view.turn_id,actor="B")
            app.authorize_resource=lambda *_:False
            with pytest.raises(PermissionError): await app.inspect_entry(view.turn_id,actor="A")
            decider.result=ChatDraft(answer="好的",memory=MemoryContent(user_preferences=("中文",)))
            first=await app.prepare_entry(ChatRequest(request_id="answer",message="请用中文"),actor="A")
            assert first.status=="ANSWERED" and first.invocation_id is None
            second=await app.prepare_entry(ChatRequest(request_id="answer2",conversation_id=first.conversation_id,message="你好"),actor="A")
            assert second.status=="ANSWERED"
            assert decider.calls[-1][1]["memory_context"].memories[0].content.user_preferences==("中文",)
            decider.result=proposal()
            rejected=await app.prepare_entry(ChatRequest(request_id="reject",message="查一下那个"),actor="A")
            assert rejected.status=="REJECTED" and rejected.result.status=="SUCCEEDED"
            with pytest.raises(ChatConflictError): await app.confirm_entry(rejected.turn_id,confirmation(rejected),actor="A")
            assert await count_cases(sessions)==1  # 只有预置案件，没有新调查。
    asyncio.run(scenario())


def test_cancel_and_permission_revocation_do_not_execute(database):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            app=service(sessions,runner,Decider(proposal()))
            cancelled=await app.prepare_entry(ChatRequest(request_id="cancel",message="调查 M"),actor="A")
            view=await app.confirm_entry(cancelled.turn_id,confirmation(cancelled,False),actor="A")
            assert view.status=="CANCELLED" and (await app.execute_entry(view.turn_id,actor="A")).status=="CANCELLED"
            with pytest.raises(ChatConflictError): await app.confirm_entry(view.turn_id,confirmation(view,True),actor="A")
            pending=await app.prepare_entry(ChatRequest(request_id="revoked",message="调查 M"),actor="A")
            await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
            app.authorize_resource=lambda *_:False
            with pytest.raises(PermissionError): await app.execute_entry(pending.turn_id,actor="A")
            assert (await app.inspect_entry(pending.turn_id,actor="A")).status=="READY"
            assert await count_cases(sessions)==0
    asyncio.run(scenario())


def test_unknown_interruption_keeps_stable_case_and_never_restarts(database,monkeypatch):
    class ProcessLost(BaseException): pass
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            decider=Decider(proposal());app=service(sessions,runner,decider)
            pending=await app.prepare_entry(ChatRequest(request_id="crash",message="调查 M"),actor="A")
            await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
            calls=[]
            async def lost(actor,case): calls.append(case);raise ProcessLost()
            monkeypatch.setattr(app,"_run_case",lost)
            with pytest.raises(ProcessLost): await app.execute_entry(pending.turn_id,actor="A")
            current=await app.inspect_entry(pending.turn_id,actor="A")
            assert current.status=="STARTED" and current.result is None
            assert current.investigation_id==pending.investigation_id and await count_cases(sessions)==1
            assert (await app.execute_entry(pending.turn_id,actor="A")).status=="STARTED" and len(calls)==1
            assert len(decider.calls)==1
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    await session.execute(text("UPDATE chat_entry_decisions SET status='SUCCEEDED' WHERE turn_id=:t"),{"t":pending.turn_id})
            assert (await app.inspect_entry(pending.turn_id,actor="A")).status=="STARTED"
    asyncio.run(scenario())


def test_concurrent_confirmation_and_execution_has_one_claim(database,monkeypatch):
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,_,runner):
            app=service(sessions,runner,Decider(proposal()))
            pending=await app.prepare_entry(ChatRequest(request_id="race",message="调查 M"),actor="A")
            async def advance():
                await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
                return await app.execute_entry(pending.turn_id,actor="A")
            results=await asyncio.gather(advance(),advance())
            assert {result.status for result in results}<={"STARTED","SUCCEEDED"}
            assert (await app.inspect_entry(pending.turn_id,actor="A")).status=="SUCCEEDED"
            assert await count_cases(sessions)==1 and agent.events.count("initial-plan")==1
    asyncio.run(scenario())


def test_model_failure_and_dependency_uncertainty_are_not_retried(database,monkeypatch):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            decider=Decider(RuntimeError("PRIVATE-provider"));app=service(sessions,runner,decider)
            request=ChatRequest(request_id="model-fail",message="hello")
            failed=await app.prepare_entry(request,actor="A")
            assert failed.status=="FAILED" and failed.decision is None
            assert await app.prepare_entry(request,actor="A")==failed and len(decider.calls)==1
            decider.result=proposal()
            pending=await app.prepare_entry(ChatRequest(request_id="uncertain",message="调查 M"),actor="A")
            await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
            async def failed_call(*_): raise RuntimeError("PRIVATE-runner")
            monkeypatch.setattr(app,"_run_case",failed_call)
            result=await app.execute_entry(pending.turn_id,actor="A")
            assert result.status=="UNCERTAIN" and result.result.error_code=="DEPENDENCY"
            assert "PRIVATE" not in result.result.model_dump_json()
            assert await app.execute_entry(pending.turn_id,actor="A")==result
            assert await count_cases(sessions)==1
    asyncio.run(scenario())



def test_runner_committed_but_response_commit_failed_can_be_located(database,monkeypatch):
    from risk_platform.conversations import action_repository as actions
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,_,runner):
            app=service(sessions,runner,Decider(proposal()))
            pending=await app.prepare_entry(ChatRequest(request_id="result-gap",message="调查 M"),actor="A")
            await app.confirm_entry(pending.turn_id,confirmation(pending),actor="A")
            original=actions.finish
            async def cannot_save(*args,**kwargs): raise RuntimeError("simulated response persistence failure")
            monkeypatch.setattr(actions,"finish",cannot_save)
            with pytest.raises(RuntimeError): await app.execute_entry(pending.turn_id,actor="A")
            monkeypatch.setattr(actions,"finish",original)
            view=await app.inspect_entry(pending.turn_id,actor="A")
            assert view.status=="STARTED" and view.investigation_id==pending.investigation_id
            assert view.result is None and await count_cases(sessions)==1
            events=list(agent.events)
            assert (await app.execute_entry(pending.turn_id,actor="A")).status=="STARTED"
            assert agent.events==events
            # 运维依据原 turn 得到案件身份后，原 Runner 本身能复用已提交事实；聊天不自动重试。
            await app._run_case("A",view.investigation_id)
            assert agent.events==events
    asyncio.run(scenario())



def test_migration_keeps_legacy_message_and_action_audit(database):
    from alembic import command
    from test_ledger_postgres import config
    from risk_platform.conversations.models import ConversationRow
    from risk_platform.conversations.contracts import ChatResponse
    from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
    from risk_platform.conversations.repository import _LegacyIntent
    command.downgrade(config(),"20261110_0016")
    saved=[]
    async def before():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            for action in ("CHAT","AUTO"):
                turn_id="legacy-"+action
                response=ChatResponse(conversation_id=turn_id,turn_id=turn_id,status="SUCCEEDED",answer="legacy answer")
                intent=_LegacyIntent(intent="CHAT")
                async with sessions.begin() as session:
                    session.add(ConversationRow(conversation_id=turn_id,owner_id="A"))
                    await session.flush()
                    row=ChatTurnRow(turn_id=turn_id,conversation_id=turn_id,actor_id="A",request_digest="a"*64,action=action,user_text="legacy message",status="STARTED")
                    session.add(row);await session.flush()
                    if action=="AUTO":
                        row.intent_payload=canonical_json(intent);row.intent_digest=input_digest(intent)
                        await session.flush()
                    from risk_platform.conversations.repository import complete_turn
                    await complete_turn(session,response)
                    saved.append((None,row.turn_id,row.response_payload,row.response_digest,row.action,row.intent_payload,row.intent_digest))
    asyncio.run(before())
    command.upgrade(config(),"head")
    async def after():
        async with resources(database,Planner()) as (sessions,_,runner):
            app=service(sessions,runner,Decider(proposal()))
            for request,turn_id,payload,digest,action,intent,intent_digest in saved:
                async with sessions() as session:
                    row=await session.get(ChatTurnRow,turn_id)
                    assert (row.response_payload,row.response_digest,row.action,row.intent_payload,row.intent_digest)==(payload,digest,action,intent,intent_digest)
                with pytest.raises(ChatConflictError): await app.inspect_entry(turn_id,actor="A")
                from risk_platform.conversations.repository import history_and_memories
                async with sessions() as session:
                    history,_=await history_and_memories(session,turn_id,"A")
                    assert history[0]["content"]=="legacy message"
    asyncio.run(after())


def test_database_rejects_wrong_confirmation_actor(database):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,_,runner):
            app=service(sessions,runner,Decider(proposal()))
            pending=await app.prepare_entry(ChatRequest(request_id="actor-check",message="调查 M"),actor="A")
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    await session.execute(text("UPDATE chat_entry_decisions SET status='READY',confirmed=TRUE,confirmation_actor='B',confirmed_at=now() WHERE turn_id=:t"),{"t":pending.turn_id})
            assert (await app.inspect_entry(pending.turn_id,actor="A")).status=="PENDING_CONFIRMATION"
    asyncio.run(scenario())


def test_http_chat_confirmation_and_original_runner(database,monkeypatch):
    import httpx
    from risk_platform.main import create_app
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,_,runner):
            decider=Decider(proposal());app=service(sessions,runner,decider)
            api=create_app(entry_services=app,authenticate=lambda _:"A")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),base_url="http://test") as client:
                body={"request_id":"http-start","message":"Investigate M"}
                pending=await client.post("/chat/messages",json=body)
                assert pending.status_code==202
                view=pending.json();assert view["status"]=="PENDING_CONFIRMATION"
                assert await count_cases(sessions)==0 and len(decider.calls)==1
                assert (await client.post("/chat/messages",json=body)).json()==view
                assert (await client.get("/chat/turns/"+view["turn_id"])).status_code==202
                confirmation_body={"invocation_id":view["invocation_id"],"decision_digest":view["decision_digest"],"confirmed":True}
                url="/chat/turns/"+view["turn_id"]+"/confirmation"
                wrong={**confirmation_body,"decision_digest":"0"*64}
                assert (await client.post(url,json=wrong)).status_code==409
                done=await client.post(url,json=confirmation_body)
                assert done.status_code==200 and done.json()["status"]=="SUCCEEDED"
                assert await count_cases(sessions)==1
                events=list(agent.events)
                assert (await client.post(url,json=confirmation_body)).json()==done.json()
                assert agent.events==events and len(decider.calls)==1
                decider.result=ToolInvocation(call_id="read",name="get_investigation_progress",arguments={"investigation_id":view["investigation_id"]})
                progress=await client.post("/chat/messages",json={"request_id":"http-read","message":"progress","investigation_id":view["investigation_id"]})
                assert progress.status_code==200 and progress.json()["status"]=="SUCCEEDED"
                decider.result=ChatDraft(answer="hello")
                answer=await client.post("/chat/messages",json={"request_id":"http-answer","message":"hi"})
                assert answer.status_code==200 and answer.json()["result"]["answer"]=="hello"
                assert (await client.post("/chat/messages",json={**body,"action":"AUTO"})).status_code==422
                decider.result=proposal()
                cancel=(await client.post("/chat/messages",json={"request_id":"http-cancel","message":"Investigate M"})).json()
                cancelled=await client.post("/chat/turns/"+cancel["turn_id"]+"/confirmation",json={"invocation_id":cancel["invocation_id"],"decision_digest":cancel["decision_digest"],"confirmed":False})
                assert cancelled.status_code==200 and cancelled.json()["status"]=="CANCELLED"
                assert await count_cases(sessions)==1
                app.authorize_resource=lambda *_:False
                assert (await client.get("/chat/turns/"+view["turn_id"])).status_code==403
    asyncio.run(scenario())
