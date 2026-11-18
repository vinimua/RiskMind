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
    def decide(self,message,*,history,memory_context,**kwargs):
        self.calls.append((history,memory_context))
        return ChatDraft(answer="Answer",memory=MemoryContent(user_preferences=("Use Chinese",)))

def service(sessions,runner,chat):
    return EntryServices(sessions=sessions,runner=runner,chat_agent=chat,
        tools_for=lambda actor,source:(KnowledgeSearchTool(KnowledgeExecutionContext(actor_id=actor,
            index_profile="parent-child-v1",scope={"model_id":"M"}),lambda *_:True,session_factory=sessions),),
        authorize_resource=lambda *_:True,authorize_conversation=lambda *_:True)

def test_alert_uses_persistent_runner_and_reuses_results(database,monkeypatch):
    agent=Planner();retrieval(monkeypatch,agent)
    async def scenario():
        async with resources(database,agent,setup=True) as (sessions,saver,runner):
            app=service(sessions,runner,Chat())
            alert=AlertRequest(alert_id="AL1",model_id="M",metric="PSI",symptom="drift",window_start="2026-11-09T00:00:00Z",window_end="2026-11-10T00:00:00Z",current_value=.3,reference_value=.1,triggered_at="2026-11-10T00:00:00Z")
            first=await app.from_alert(alert,actor="A")
            events=list(agent.events)
            assert (await app.from_alert(alert,actor="A"))["run_id"]==first["run_id"]
            assert agent.events==events
            from risk_platform.workflow.persistence.repository import LedgerConflictError
            with pytest.raises(LedgerConflictError):
                await app.from_alert(alert.model_copy(update={"symptom":"different"}),actor="A")
    asyncio.run(scenario())


def test_progress_authorizes_latest_round_without_loading_first_round(database):
    from risk_platform.workflow.persistence import repository as ledger
    from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord
    from test_runtime import planning
    reads = []
    async def scenario():
        async with resources(database, Planner(), setup=True) as (sessions, saver, runner):
            async with sessions.begin() as session:
                await ledger.register_investigation(session, InvestigationRecord(
                    investigation_id="I", created_by="A", planning_input=planning()))
                for number in (1, 2):
                    await ledger.register_round(session, RoundRecord(
                        investigation_id="I", round_id=f"R{number}", round_number=number))
            app = service(sessions, runner, Chat())
            app.authorize_resource = lambda actor, action, resource: reads.append((action, resource.round_id)) or True
            assert "暂无已提交结果" in await app._progress("A", "I")
            assert reads == [("investigation:read", "R2")]
            with pytest.raises(chat_store.ChatNotFoundError):
                await app._progress("A", "missing")
    from risk_platform.conversations import repository as chat_store
    asyncio.run(scenario())
