"""真实 PostgreSQL：文档隔离、修订、幂等、聊天接入及维护恢复。"""
import asyncio
import pytest
from sqlalchemy import select, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from test_ledger_postgres import database, pytestmark
from test_entrypoints_postgres import service
from risk_platform.memory import personal
from risk_platform.memory.models import PersonalNoteRevisionRow, MemoryMaintenanceRow
from risk_platform.memory.personal_contracts import NoteContent, NoteChange, MemorySettings
from risk_platform.conversations.contracts import ChatDraft, ChatRequest
from risk_platform.conversations.repository import ChatConflictError, ChatNotFoundError


DOC = NoteContent(title="代码讲解",description="解释代码时使用",content="沿调用链解释")


def test_notes_isolate_users_and_preserve_revisions(database):
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                note=await personal.save_note(s,"A",DOC,source_id="create")
            async with sessions.begin() as s:
                assert await personal.save_note(s,"A",DOC,source_id="create")==note
            async with sessions() as s:
                assert await personal.catalogue(s,"B")==[]
                with pytest.raises(ChatNotFoundError): await personal.read_note(s,"B",note.note_id)
            async def update(source):
                async with sessions.begin() as s:
                    return await personal.save_note(s,"A",DOC.model_copy(update={"content":"详细说明"}),
                        source_id=source,note_id=note.note_id,expected_version=1)
            results=await asyncio.gather(update("u1"),update("u2"),return_exceptions=True)
            assert sum(isinstance(x,ChatConflictError) for x in results)==1
            async with sessions() as s:
                assert len(list(await s.scalars(select(PersonalNoteRevisionRow))))==2
                cols=await s.run_sync(lambda conn: [x["name"] for x in inspect(conn.connection()).get_columns("chat_turns")])
                assert "memory_payload" not in cols
            async with sessions.begin() as s:
                await personal.set_settings(s,"A",MemorySettings(read_enabled=False))
            async with sessions() as s:
                assert await personal.catalogue(s,"A",for_model=True)==[]
                with pytest.raises(PermissionError): await personal.read_note(s,"A",note.note_id,for_model=True)
                assert (await personal.read_note(s,"A",note.note_id)).version==2
        finally: await engine.dispose()
    asyncio.run(scenario())


class Agent:
    def __init__(self): self.calls=0;self.writes=0;self.fail=False;self.recalled=[]
    async def decide(self,message,**kwargs):
        self.calls+=1
        for item in kwargs["memory_index"]:
            self.recalled.append(await kwargs["read_memory"](item["note_id"]))
        return ChatDraft(answer="回答已保存",memory_candidate="希望沿调用链解释")
    async def maintain(self,candidate,*,index,read_memory):
        self.writes+=1
        if self.fail: raise RuntimeError("模型失败")
        if index:
            await read_memory(index[0]["note_id"])
            return NoteChange(action="NONE")
        return NoteChange(action="CREATE",document=DOC)


def test_chat_cross_session_and_failed_maintenance_retry(database):
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        agent=Agent();app=service(sessions,None,agent)
        try:
            first=await app.prepare_entry(ChatRequest(request_id="one",message="记住"),actor="A")
            assert first.result.answer=="回答已保存"
            second=await app.prepare_entry(ChatRequest(request_id="two",message="新会话"),actor="A")
            assert second.conversation_id != first.conversation_id
            assert agent.recalled[-1].content == DOC.content
            async with sessions() as s:
                assert len(await personal.catalogue(s,"A"))==1
            agent.fail=True
            third=await app.prepare_entry(ChatRequest(request_id="three",message="新要求"),actor="A")
            assert third.result.status=="SUCCEEDED"
            async with sessions() as s:
                assert (await s.get(MemoryMaintenanceRow,third.turn_id)).status=="FAILED"
            agent.fail=False
            await asyncio.gather(*(personal.maintenance(sessions,"A",third.turn_id,agent,timeout_seconds=3) for _ in range(2)))
            assert agent.calls==3
            assert await app.prepare_entry(ChatRequest(request_id="three",message="新要求"),actor="A")==third
            async with sessions() as s:
                assert len(await personal.catalogue(s,"A"))==1
                assert (await s.get(MemoryMaintenanceRow,third.turn_id)).status=="DONE"
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_management_http_identity_versions_settings_and_idempotency(database):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport
    from risk_platform.api.routes.entrypoints import router
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        app=FastAPI();app.include_router(router)
        app.state.entry_services=service(sessions,None,Agent())
        app.state.authenticate=lambda request:request.headers["X-Actor"]
        try:
            async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test",headers={"X-Actor":"A"}) as client:
                body=DOC.model_dump()|{"request_id":"create"}
                first=await client.post("/memory/notes",json=body)
                assert first.status_code==200
                note=first.json();url="/memory/notes/"+note["note_id"]
                assert (await client.post("/memory/notes",json=body)).json()==note
                assert (await client.post("/memory/notes",json=body|{"content":"changed"})).status_code==409
                assert (await client.get(url,headers={"X-Actor":"B"})).status_code==404
                update=DOC.model_dump()|{"request_id":"stop","expected_version":1,"active":False}
                stopped=await client.put(url,json=update)
                assert stopped.status_code==200 and stopped.json()["version"]==2
                assert (await client.put(url,json=update)).json()==stopped.json()
                revisions=(await client.get(url+"/revisions")).json()
                assert [x["version"] for x in revisions]==[1,2]
                assert (await client.put(url,json=update|{"request_id":"stale"})).status_code==409
                assert (await client.get("/memory/notes")).json()[0]["active"] is False
                settings=await client.put("/memory/settings",json={"read_enabled":False,"auto_write_enabled":False})
                assert settings.status_code==200 and (await client.get("/memory/settings")).json()==settings.json()
                assert (await client.post("/memory/maintenance/missing/retry")).status_code==404
                assert (await client.post("/memory/notes",json=body|{"owner_id":"B"})).status_code==422
        finally: await engine.dispose()
    asyncio.run(scenario())


def test_upgrade_discards_old_memory_but_preserves_answer(monkeypatch):
    import os
    import json
    import hashlib
    from uuid import uuid4
    from sqlalchemy import create_engine, make_url
    from alembic import command
    from test_ledger_postgres import config
    from risk_platform.conversations.models import ConversationRow, ChatTurnRow, EntryDecisionRow
    from risk_platform.conversations.contracts import ChatResponse
    from risk_platform.conversations.repository import complete_turn
    schema="test_personal_upgrade_"+uuid4().hex
    url=make_url(os.environ["RISK_DATABASE_URL"])
    admin=create_engine(url)
    isolated=url.update_query_dict({"options":f"-csearch_path={schema}"})
    try:
        with admin.begin() as c: c.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("RISK_DATABASE_URL",isolated.render_as_string(hide_password=False))
        command.upgrade(config(),"20261121_0022")
        answer='中文正文与 "引号"\n下一行'
        request_payload=ChatRequest(request_id="old",conversation_id="C",message="原问题").model_dump_json()
        async def seed_old():
            engine=create_async_engine(isolated);sessions=async_sessionmaker(engine,expire_on_commit=False)
            try:
                async with sessions.begin() as s:
                    s.add(ConversationRow(conversation_id="C",owner_id="A"));await s.flush()
                    # 此处刻意写迁移前的列；生产 ORM 只保留新契约，不兼容旧数据库。
                    await s.execute(text("""INSERT INTO chat_turns
                        (turn_id,conversation_id,actor_id,request_digest,action,user_text,status)
                        VALUES ('T','C','A',:digest,'ENTRY_DECISION',:message,'STARTED')"""),
                        {"digest":hashlib.sha256(request_payload.encode()).hexdigest(),"message":"原问题"})
                    payload=json.dumps(dict(answer=answer,memory={"user_preferences":["旧偏好"]}),
                                       ensure_ascii=False,sort_keys=True,separators=(",",":"))
                    s.add(EntryDecisionRow(turn_id="T",request_payload=request_payload,decision_payload=payload,
                        decision_digest=hashlib.sha256(payload.encode()).hexdigest(),status="ANSWERED"))
                    await s.flush()
                    from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
                    response=ChatResponse(conversation_id="C",turn_id="T",status="SUCCEEDED",answer=answer)
                    await s.execute(text("""UPDATE chat_turns SET status='SUCCEEDED',response_payload=:payload,
                        response_digest=:digest,ended_at=CURRENT_TIMESTAMP WHERE turn_id='T'"""),
                        {"payload":canonical_json(response),"digest":input_digest(response)})
            finally: await engine.dispose()
        asyncio.run(seed_old())
        command.upgrade(config(),"head")
        async def verify():
            engine=create_async_engine(isolated);sessions=async_sessionmaker(engine,expire_on_commit=False)
            try:
                result=await service(sessions,None,Agent()).inspect_entry("T",actor="A")
                assert result.result.answer==answer and result.decision.memory_candidate is None
                async with sessions() as s:
                    assert await personal.catalogue(s,"A")==[]
            finally: await engine.dispose()
        asyncio.run(verify())
    finally:
        # 名称仅由固定测试前缀和 UUID 构造，不接受外部标识。
        with admin.begin() as c: c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_disabled_maintenance_and_unread_update_leave_notes_unchanged(database):
    class Unread(Agent):
        async def maintain(self,candidate,*,index,read_memory):
            return NoteChange(action="UPDATE",note_id=index[0]["note_id"],expected_version=1,document=DOC)
    async def scenario():
        engine=create_async_engine(database);sessions=async_sessionmaker(engine,expire_on_commit=False)
        try:
            async with sessions.begin() as s:
                await personal.save_note(s,"A",DOC,source_id="manual")
            app=service(sessions,None,Unread())
            result=await app.prepare_entry(ChatRequest(request_id="unread",message="记住"),actor="A")
            async with sessions() as s:
                assert (await s.get(MemoryMaintenanceRow,result.turn_id)).status=="FAILED"
                assert (await personal.catalogue(s,"A"))[0]["version"]==1
            async with sessions.begin() as s:
                await personal.set_settings(s,"A",MemorySettings(auto_write_enabled=False))
            result=await app.prepare_entry(ChatRequest(request_id="disabled",message="记住"),actor="A")
            async with sessions() as s:
                assert (await s.get(MemoryMaintenanceRow,result.turn_id)).status=="DISABLED"
        finally: await engine.dispose()
    asyncio.run(scenario())
