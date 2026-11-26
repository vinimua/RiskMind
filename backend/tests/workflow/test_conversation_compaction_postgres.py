"""真实数据库：摘要覆盖、原文、失败事务与中断后继续使用；模型均为替身。"""
import asyncio
import hashlib
import json
import pytest
from sqlalchemy import select, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from test_ledger_postgres import database, pytestmark
from test_entrypoints_postgres import service
from risk_platform.conversations import action_repository as actions, repository, context
from risk_platform.conversations import agent as inputs
from risk_platform.conversations.contracts import ChatRequest, ChatDraft, ChatResponse, ToolInvocation
from risk_platform.conversations.models import ChatTurnRow, ConversationSummaryRow


async def seed_history(sessions, count=8, *, long=False):
    conversation_id=None
    ids=[]
    for number in range(count):
        request=ChatRequest(request_id=f"old-{number}",conversation_id=conversation_id,
            message=("original "*450 if long else "question-")+str(number))
        async with sessions.begin() as session:
            turn,_=await actions.begin(session,request,actor="A",authorize=lambda *_:True)
            conversation_id=turn.conversation_id
            await actions.save_decision(session,turn,request,
                ChatDraft(answer=("response "*450 if long else "answer-")+str(number)))
            ids.append(turn.turn_id)
    return conversation_id,ids


class Agent:
    def __init__(self): self.summaries=[];self.answers=[];self.fail_summary=False;self.fail_answer=False
    async def summarize(self, previous, turns):
        self.summaries.append((previous,list(turns)))
        if self.fail_summary: raise RuntimeError("summary unavailable")
        return "已讨论目标与限制，仍待确认。批次："+str(len(self.summaries))
    async def decide(self,message,**kwargs):
        self.answers.append(kwargs)
        if self.fail_answer: raise RuntimeError("answer unavailable")
        return ChatDraft(answer="继续回答")


def test_full_history_no_longer_discards_old_turns_or_clips_text(database):
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            cid,ids=await seed_history(sessions,12)
            async with sessions.begin() as s:
                request=ChatRequest(request_id="case-result",conversation_id=cid,message="案件 I 的进度")
                turn,_=await actions.begin(s,request,actor="A",authorize=lambda *_:True)
                await actions.save_decision(s,turn,request,ToolInvocation(call_id="call",name="get_investigation_progress",
                    arguments={"investigation_id":"I"}))
                await actions.start(s,turn)
                await actions.finish(s,turn,ChatResponse(conversation_id=cid,turn_id=turn.turn_id,
                    status="SUCCEEDED",answer="CASE_SECRET",investigation_id="I"))
            agent=Agent();app=service(sessions,None,agent)
            result=await app.prepare_entry(ChatRequest(request_id="new",conversation_id=cid,message="继续"),actor="A")
            assert result.result.status=="SUCCEEDED" and not agent.summaries
            assert len(agent.answers[-1]["history"])==24
            assert agent.answers[-1]["history"][0]["content"]=="question-0"
            assert "CASE_SECRET" not in str(agent.answers)
            async with sessions() as s:
                rows=list(await s.scalars(select(ChatTurnRow).where(ChatTurnRow.conversation_id==cid).order_by(ChatTurnRow.turn_number)))
                assert [r.turn_number for r in rows]==list(range(1,15))
                page=await repository.history_turns(s,cid,"A",after_turn=12,limit=1)
                assert page=={"turns":[],"through_turn_number":13}
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_compaction_preserves_raw_text_reuses_summary_and_exposes_authorized_history(database,monkeypatch):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from risk_platform.api.routes.entrypoints import router
    monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",3000)
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            cid,ids=await seed_history(sessions,long=True)
            agent=Agent();app=service(sessions,None,agent)
            result=await app.prepare_entry(ChatRequest(request_id="compact",conversation_id=cid,message="继续"),actor="A")
            assert result.result.status=="SUCCEEDED" and agent.summaries
            async with sessions() as s:
                summary=await context.latest_summary(s,cid,"A")
                assert 0<summary.through_turn_number<=6
                assert summary.through_turn_id==ids[summary.through_turn_number-1]
                raw=(await repository.history_turns(s,cid,"A"))["turns"]
                assert len(raw)==9 and len(raw[0]["user"])>2000
                expected=context.history_messages(raw[summary.through_turn_number:8])
                assert agent.answers[-1]["history"]==expected
                assert agent.answers[-1]["summary"]==summary.content
            for _,batch in agent.summaries:
                assert all(set(t)=={"turn_id","turn_number","user","assistant"} for t in batch)
            calls=len(agent.summaries)
            monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",24000)
            await app.prepare_entry(ChatRequest(request_id="next",conversation_id=cid,message="下一问"),actor="A")
            assert len(agent.summaries)==calls
            assert agent.answers[-1]["summary"]==summary.content
            with pytest.raises(DBAPIError):
                async with sessions.begin() as s:
                    await s.execute(text("UPDATE chat_conversation_summaries SET content='modified'"))
            api=FastAPI();api.include_router(router);api.state.entry_services=app
            api.state.authenticate=lambda request:request.headers["X-Actor"]
            async with AsyncClient(transport=ASGITransport(app=api),base_url="http://test",headers={"X-Actor":"A"}) as client:
                path=f"/chat/conversations/{cid}/history"
                response=await client.get(path,params={"limit":2})
                assert response.status_code==200 and len(response.json()["turns"])==2
                assert len(response.json()["turns"][0]["user"])>2000
                page=await client.get(path,params={"after_turn":response.json()["through_turn_number"],"limit":2})
                assert page.json()["turns"][0]["turn_number"]==3
                assert (await client.get(path,headers={"X-Actor":"B"})).status_code==404
        finally: await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure",["model", "later_batch", "commit", "oversized"])
def test_failure_never_advances_boundary_or_calls_answer(database,monkeypatch,failure):
    monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",3000)
    if failure=="oversized": monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",1000)
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        agent=Agent();agent.fail_summary=failure=="model"
        if failure=="later_batch":
            summarize=agent.summarize
            async def fail_second(previous,turns):
                if agent.summaries: raise RuntimeError("second batch unavailable")
                return await summarize(previous,turns)
            agent.summarize=fail_second
        def reject_summary(session,*_):
            if any(isinstance(row,ConversationSummaryRow) for row in session.new):
                raise RuntimeError("summary commit rejected")
        try:
            cid,_=await seed_history(sessions,long=True)
            if failure=="commit": event.listen(sessions.class_.sync_session_class,"before_flush",reject_summary)
            app=service(sessions,None,agent)
            result=await app.prepare_entry(ChatRequest(request_id="failure",conversation_id=cid,
                message="word "*1500 if failure=="oversized" else "继续"),actor="A")
            assert result.result.status=="FAILED" and not agent.answers
            assert result.result.error_code=={"model":"COMPACTION_FAILED","later_batch":"COMPACTION_FAILED","commit":"DEPENDENCY","oversized":"CONTEXT_BUDGET"}[failure]
            async with sessions() as s:
                assert await context.latest_summary(s,cid,"A") is None
                assert len((await repository.history_turns(s,cid,"A"))["turns"])==8
            if failure=="oversized": assert not agent.summaries
            if failure=="later_batch": assert len(agent.summaries)==1
        finally:
            if failure=="commit": event.remove(sessions.class_.sync_session_class,"before_flush",reject_summary)
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["concurrent_summary", "permission_revoked"])
def test_external_wait_cannot_overwrite_summary_or_bypass_revocation(database, monkeypatch, change):
    """两次读取同一旧版本，或模型等待期间撤权，提交必须重新核对。"""
    monkeypatch.setattr(inputs, "CHAT_INPUT_LIMIT", 3000)

    async def scenario():
        engine = create_async_engine(database)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            cid, _ = await seed_history(sessions, long=True)
            requests = [ChatRequest(request_id="race", conversation_id=cid, message="继续")]
            ids = []
            async with sessions.begin() as s:
                for request in requests:
                    turn, _ = await actions.begin(s, request, actor="A", authorize=lambda *_: True)
                    ids.append(turn.turn_id)
                turns = (await repository.history_turns(s, cid, "A"))["turns"]
            # 入口先禁止同会话的另一新请求；下面再验证同一旧版本的竞争提交。
            with pytest.raises(repository.ChatBusyError):
                async with sessions.begin() as s:
                    await actions.begin(s, ChatRequest(request_id="another", conversation_id=cid,
                        message="另一请求"), actor="A", authorize=lambda *_: True)
            ready = asyncio.Event()
            entered = 0
            allowed = True

            class WaitingAgent(Agent):
                async def summarize(self, previous, batch):
                    nonlocal entered, allowed
                    entered += 1
                    if change == "permission_revoked":
                        allowed = False
                    elif entered >= 2:
                        ready.set()
                    else:
                        await asyncio.wait_for(ready.wait(), 10)
                    return await super().summarize(previous, batch)

            async def compact():
                return await context.compact_history(sessions, actor="A", turn_id=ids[0],
                    request=requests[0], agent=WaitingAgent(), authorize=lambda *_: allowed,
                    previous=None, turns=list(turns), memory_index=[], compaction_budget={"remaining": 8})

            if change == "permission_revoked":
                with pytest.raises(PermissionError):
                    await compact()
            else:
                outcomes = await asyncio.gather(compact(), compact(), return_exceptions=True)
                assert sum(isinstance(value, repository.ChatConflictError) for value in outcomes) == 1
                assert sum(isinstance(value, tuple) for value in outcomes) == 1
            async with sessions() as s:
                saved = list(await s.scalars(select(ConversationSummaryRow).where(
                    ConversationSummaryRow.conversation_id == cid)))
                assert len(saved) == (0 if change == "permission_revoked" else 1)
                assert len((await repository.history_turns(s, cid, "A"))["turns"]) == 8
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_saved_summary_survives_failure_before_answer(database,monkeypatch):
    monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",3000)
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            cid,_=await seed_history(sessions,long=True)
            agent=Agent();agent.fail_answer=True;app=service(sessions,None,agent)
            failed=await app.prepare_entry(ChatRequest(request_id="lost-answer",conversation_id=cid,message="继续"),actor="A")
            assert failed.result.status=="FAILED"
            async with sessions() as s: saved=(await context.latest_summary(s,cid,"A")).content
            calls=len(agent.summaries);agent.fail_answer=False
            monkeypatch.setattr(inputs,"CHAT_INPUT_LIMIT",24000)
            result=await app.prepare_entry(ChatRequest(request_id="resume",conversation_id=cid,message="继续"),actor="A")
            assert result.result.status=="SUCCEEDED" and len(agent.summaries)==calls
            assert agent.answers[-1]["summary"]==saved
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_read_trace_survives_answer_failure_and_is_immutable(database):
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.memory import personal
    from risk_platform.memory.personal_contracts import NoteContent
    from test_personal_memory import SequenceModel
    from tool_model_helpers import tool_response
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                note=await personal.save_note(s,"A",NoteContent(title="偏好",description="讲解",content="中文优先"),source_id="seed")
            model=SequenceModel([tool_response("read_memory",{"note_id":note.note_id}),RuntimeError("answer failed")])
            app=service(sessions,None,ChatAgent("test",tool_model=model))
            response=await app.prepare_entry(ChatRequest(request_id="read-then-fail",message="按我的偏好解释"),actor="A")
            assert response.result.status=="FAILED"
            async with sessions() as s:
                pairs=await repository.read_interactions(s,response.turn_id)
                assert len(pairs)==1 and "中文优先" in pairs[0][1]["content"]
            with pytest.raises(DBAPIError):
                async with sessions.begin() as s:
                    await s.execute(text("UPDATE chat_read_interactions SET payload='[]'"))
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_note_body_is_not_carried_into_future_chat_summary(database):
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.memory import personal
    from risk_platform.memory.personal_contracts import NoteContent, MemorySettings
    from test_personal_memory import SequenceModel,answer
    from tool_model_helpers import tool_response
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                note=await personal.save_note(s,"A",NoteContent(title="偏好",description="讲解",content="PRIVATE_NOTE_BODY"),source_id="seed")
            model=SequenceModel([tool_response("read_memory",{"note_id":note.note_id}),answer()])
            app=service(sessions,None,ChatAgent("test",tool_model=model))
            response=await app.prepare_entry(ChatRequest(request_id="read",message="按偏好解释"),actor="A")
            assert response.result.status=="SUCCEEDED"
            async with sessions() as s:
                turns=(await repository.history_turns(s,response.conversation_id,"A",include_reads=True))["turns"]
                assert turns[0]["read_messages"] and "PRIVATE_NOTE_BODY" not in str(turns)
            async with sessions.begin() as s:
                await personal.set_settings(s,"A",MemorySettings(read_enabled=False))
            async with sessions() as s:
                turns=(await repository.history_turns(s,response.conversation_id,"A",include_reads=True))["turns"]
                assert "read_messages" not in turns[0]
                with pytest.raises(repository.ChatNotFoundError):
                    await repository.history_turns(s,response.conversation_id,"B",include_reads=True)
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_explicit_snapshot_read_returns_old_note_only_while_current_access_remains(database):
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.memory import personal
    from risk_platform.memory.personal_contracts import NoteContent, MemorySettings
    from test_personal_memory import SequenceModel,answer
    from tool_model_helpers import tool_response
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                note=await personal.save_note(s,"A",NoteContent(title="偏好",description="讲解",content="OLD_BODY"),source_id="seed")
            history_call=tool_response("read_chat_history",dict(from_turn=1,to_turn=1,read_interaction=0))
            model=SequenceModel([tool_response("read_memory",{"note_id":note.note_id}),answer(),history_call,answer(),history_call])
            app=service(sessions,None,ChatAgent("test",tool_model=model))
            first=await app.prepare_entry(ChatRequest(request_id="first",message="解释"),actor="A")
            async with sessions.begin() as s:
                await personal.save_note(s,"A",NoteContent(title="偏好",description="讲解",content="NEW_BODY"),
                    source_id="update",note_id=note.note_id,expected_version=1)
            replay=await app.prepare_entry(ChatRequest(request_id="replay",conversation_id=first.conversation_id,message="当时读的什么？"),actor="A")
            assert replay.result.status=="SUCCEEDED"
            async with sessions() as s:
                pair=(await repository.read_interactions(s,replay.turn_id))[0]
                body=json.loads(pair[1]["content"])
                assert body["historical_not_current"] and body["note"]["version"]==1
                assert body["note"]["content"]=="OLD_BODY"
            async with sessions.begin() as s:
                await personal.set_settings(s,"A",MemorySettings(read_enabled=False))
            denied=await app.prepare_entry(ChatRequest(request_id="denied",conversation_id=first.conversation_id,message="再读当时正文"),actor="A")
            assert denied.result.status=="FAILED"
        finally: await engine.dispose()
    asyncio.run(scenario())
