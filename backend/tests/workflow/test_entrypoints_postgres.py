import asyncio
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import Agent, resources, retrieval
from risk_platform.entrypoints import EntryServices, AlertRequest
from risk_platform.conversations.contracts import ChatRequest, ChatDraft
from risk_platform.conversations.models import ChatTurnRow
from risk_platform.conversations.repository import ChatConflictError, ChatBusyError, claim_turn
from risk_platform.memory.contracts import MemoryContent
from risk_platform.workflow.contracts import InvestigationPlan, InvestigationTask
from risk_platform.workflow.persistence.models import InvestigationRow
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.execution.contracts import KnowledgeExecutionContext

class Planner(Agent):
    def initial_plan(self, source):
        self.events.append("initial-plan")
        return InvestigationPlan(goal="Investigate",tasks=[InvestigationTask(task_id=f"T{i}",objective="history",
            capability="KNOWLEDGE_RETRIEVAL",expected_result="clues") for i in range(2)])

class Chat:
    def __init__(self): self.calls=[]
    def reply(self,message,*,history,memory_context):
        self.calls.append((history,memory_context))
        return ChatDraft(answer="Answer",memory=MemoryContent(user_preferences=("Use Chinese",)))

def service(sessions,runner,chat):
    return EntryServices(sessions=sessions,runner=runner,chat_agent=chat,
        tools_for=lambda actor,source:(KnowledgeSearchTool(KnowledgeExecutionContext(actor_id=actor,
            index_profile="parent-child-v1",scope={"model_id":"M"}),lambda *_:True,session_factory=sessions),),
        authorize_resource=lambda *_:True,authorize_conversation=lambda *_:True)

def test_alert_and_chat_start_use_same_persistent_runner(database,monkeypatch):
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,saver,runner):
            app=service(sessions,runner,Chat())
            alert=AlertRequest(alert_id="AL1",model_id="M",metric="PSI",symptom="drift",time_window="today",
                current_value=.3,reference_value=.1,triggered_at="2026-11-10T00:00:00Z")
            first=await app.from_alert(alert,actor="A")
            events=list(agent.events)
            assert (await app.from_alert(alert,actor="A"))["run_id"]==first["run_id"]
            assert agent.events==events
            from risk_platform.workflow.persistence.repository import LedgerConflictError
            with pytest.raises(LedgerConflictError):
                await app.from_alert(alert.model_copy(update={"symptom":"different"}),actor="A")
            request=ChatRequest(request_id="start",message="Investigate drift",action="START_INVESTIGATION",model_id="M")
            response=await app.chat(request,actor="A")
            assert response.status=="SUCCEEDED", response
            assert response.investigation_id!=first["investigation_id"]
            events=list(agent.events)
            assert await app.chat(request,actor="A")==response and agent.events==events
            app.authorize_resource=lambda actor,action,resource:action=="investigation:read"
            read=await app.chat(ChatRequest(request_id="read",message="Progress?",action="READ_INVESTIGATION",
                investigation_id=response.investigation_id),actor="A")
            assert read.status=="SUCCEEDED" and agent.events==events
            async with sessions() as session:
                assert len(list(await session.scalars(select(InvestigationRow))))==2
    asyncio.run(scenario())

def test_chat_memory_reuse_ownership_conflict_busy_and_immutability(database):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,saver,runner):
            chat=Chat();app=service(sessions,runner,chat)
            request=ChatRequest(request_id="one",message="Speak Chinese")
            first=await app.chat(request,actor="A")
            assert first.status=="SUCCEEDED"
            assert await app.chat(request,actor="A")==first and len(chat.calls)==1
            with pytest.raises(ChatConflictError): await app.chat(request.model_copy(update={"message":"changed"}),actor="A")
            with pytest.raises(PermissionError):
                await app.chat(ChatRequest(request_id="steal",conversation_id=first.conversation_id,message="hi"),actor="B")
            second=await app.chat(ChatRequest(request_id="two",conversation_id=first.conversation_id,message="hello"),actor="A")
            assert second.status=="SUCCEEDED"
            assert chat.calls[1][0][0]["content"]=="Speak Chinese"
            assert chat.calls[1][1].memories[0].content.user_preferences==("Use Chinese",)
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    await session.execute(text("UPDATE chat_turns SET user_text='tamper'"))
            active=ChatRequest(request_id="active",conversation_id=first.conversation_id,message="pending")
            async with sessions.begin() as session:
                await claim_turn(session,active,actor="A",authorize_conversation=lambda *_:True)
            with pytest.raises(ChatBusyError): await app.chat(active,actor="A")
            with pytest.raises(ChatBusyError):
                await app.chat(active.model_copy(update={"request_id":"another"}),actor="A")
            assert len(chat.calls)==2
    asyncio.run(scenario())


def test_chat_failure_keeps_message_without_false_memory_and_denies_case_creation(database):
    class BrokenChat:
        def reply(self,*args,**kwargs): raise RuntimeError("private-provider-token")
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,saver,runner):
            app=service(sessions,runner,BrokenChat())
            request=ChatRequest(request_id="broken",message="hello")
            response=await app.chat(request,actor="A")
            assert response.status=="FAILED" and response.error_code=="DEPENDENCY"
            assert "private" not in response.model_dump_json()
            assert await app.chat(request,actor="A")==response
            async with sessions() as session:
                row=await session.get(ChatTurnRow,response.turn_id)
                assert row.user_text=="hello" and row.memory_payload is None
            app.authorize_resource=lambda *_:False
            denied=await app.chat(ChatRequest(request_id="denied",message="investigate",action="START_INVESTIGATION",model_id="M"),actor="A")
            assert denied.status=="FAILED" and denied.error_code=="FORBIDDEN"
            async with sessions() as session:
                assert not list(await session.scalars(select(InvestigationRow)))
    asyncio.run(scenario())


def test_concurrent_same_message_has_one_claim(database):
    async def scenario():
        async with resources(database,Planner(),setup=True) as (sessions,saver,runner):
            request=ChatRequest(request_id="race",message="same message")
            async def claim():
                async with sessions.begin() as session:
                    return await claim_turn(session,request,actor="A",authorize_conversation=lambda *_:True)
            results=await asyncio.gather(claim(),claim(),return_exceptions=True)
            assert sum(isinstance(result,tuple) for result in results)==1
            assert sum(isinstance(result,ChatBusyError) for result in results)==1
            async with sessions() as session:
                assert len(list(await session.scalars(select(ChatTurnRow))))==1
    asyncio.run(scenario())
