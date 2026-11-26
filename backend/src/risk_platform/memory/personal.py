"""个人笔记的所有权、修订及维护提交；模型调用不持有事务。"""
import asyncio
from uuid import uuid4
from sqlalchemy import select, text
from risk_platform.conversations.repository import ChatConflictError, ChatNotFoundError
from risk_platform.workflow.persistence.contracts import canonical_json
from .models import PersonalNoteRow, PersonalNoteRevisionRow, PersonalMemorySettingsRow, MemoryMaintenanceRow
from .personal_contracts import PersonalNote, NoteContent, MemorySettings, NoteChange


async def _lock(session, actor):
    # 同一用户的设置修改与笔记提交串行化；等待模型期间不持有锁。
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                          {"key": "personal-memory:" + actor})


def _document(row):
    return PersonalNote(note_id=row.note_id, title=row.title, description=row.description,
                        content=row.content, version=row.version, active=row.active)


async def settings(session, actor):
    row = await session.get(PersonalMemorySettingsRow, actor)
    return MemorySettings(read_enabled=row.read_enabled, auto_write_enabled=row.auto_write_enabled) if row else MemorySettings()


async def set_settings(session, actor, value):
    await _lock(session, actor)
    row = await session.get(PersonalMemorySettingsRow, actor)
    if row is None:
        session.add(PersonalMemorySettingsRow(owner_id=actor, **value.model_dump()))
    else:
        row.read_enabled, row.auto_write_enabled = value.read_enabled, value.auto_write_enabled
    await session.flush()
    return value


async def catalogue(session, actor, *, query=None, for_model=False):
    if for_model and not (await settings(session, actor)).read_enabled:
        return []
    statement = select(PersonalNoteRow).where(PersonalNoteRow.owner_id == actor)
    if for_model:
        statement = statement.where(PersonalNoteRow.active.is_(True))
    if query:
        # 参数绑定并转义通配符；搜索不扩大用户范围。
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        statement = statement.where(PersonalNoteRow.title.ilike(pattern, escape="\\") |
                                    PersonalNoteRow.description.ilike(pattern, escape="\\"))
    rows = list(await session.scalars(statement.order_by(PersonalNoteRow.title).limit(51)))
    if for_model and len(rows) > 50:
        raise ChatConflictError("记忆目录超过输入上限，请先整理个人笔记")
    return [dict(note_id=r.note_id, title=r.title, description=r.description,
                 version=r.version, active=r.active) for r in rows[:50]]


async def read_note(session, actor, note_id, *, for_model=False):
    row = await session.get(PersonalNoteRow, note_id)
    if row is None or row.owner_id != actor:
        raise ChatNotFoundError("个人笔记不可访问")
    if for_model and (not row.active or not (await settings(session, actor)).read_enabled):
        raise PermissionError("个人记忆读取已关闭或笔记已停用")
    return _document(row)


async def save_note(session, actor, document, *, source_id, note_id=None, expected_version=None, active=True):
    await _lock(session, actor)
    prior = await session.scalar(select(PersonalNoteRevisionRow).join(PersonalNoteRow,
        PersonalNoteRevisionRow.note_id == PersonalNoteRow.note_id).where(
        PersonalNoteRevisionRow.source_id == source_id, PersonalNoteRow.owner_id == actor))
    if prior is not None:
        saved = PersonalNote.model_validate_json(prior.payload)
        if (saved.model_dump(include={"title","description","content"}) != document.model_dump()
            or saved.active != active or saved.version != (expected_version + 1 if expected_version is not None else 1)
            or (note_id is not None and saved.note_id != note_id)):
            raise ChatConflictError("同一请求不能更换笔记修改内容")
        return saved
    if note_id is None:
        duplicate = await session.scalar(select(PersonalNoteRow).where(
            PersonalNoteRow.owner_id == actor, PersonalNoteRow.title == document.title))
        if duplicate is not None:
            raise ChatConflictError("同标题笔记已存在，请读取后修改")
        row = PersonalNoteRow(note_id="note-" + uuid4().hex, owner_id=actor,
            **document.model_dump(), version=1, active=active)
        session.add(row)
    else:
        row = await session.get(PersonalNoteRow, note_id, populate_existing=True)
        if row is None or row.owner_id != actor:
            raise ChatNotFoundError("个人笔记不可访问")
        if row.version != expected_version:
            raise ChatConflictError("笔记版本已变化，请重新读取")
        duplicate = await session.scalar(select(PersonalNoteRow.note_id).where(
            PersonalNoteRow.owner_id == actor, PersonalNoteRow.title == document.title,
            PersonalNoteRow.note_id != note_id))
        if duplicate is not None:
            raise ChatConflictError("同标题笔记已存在")
        row.title, row.description, row.content = document.title, document.description, document.content
        row.version += 1
        row.active = active
    await session.flush()
    note = _document(row)
    session.add(PersonalNoteRevisionRow(note_id=row.note_id, version=row.version,
        source_id=source_id, payload=canonical_json(note)))
    await session.flush()
    return note


# 读取该 turn_id 的维护候选
#     ↓
# 核对属于当前用户；处理完的直接返回已有结果
#     ↓
# 读取维护开关和笔记目录
#     ↓
# 调用 agent.maintain()
# # 模型通过 _dialogue() 按需读取笔记
# personal.maintenance() → 负责整次维护业务与数据库提交
# agent.maintain()       → 让模型提出修改建议
# agent._dialogue()      → 负责模型与记忆读取工具之间的循环
#     ↓
# 取得 NONE／CREATE／UPDATE／DEACTIVATE 建议
#     ↓
# 程序检查读取版本与权限
#     ↓
# 保存笔记修订，并把维护记录标为 DONE
async def maintenance(sessions, actor, turn_id, agent, *, timeout_seconds):
    """显式重试复用原回答；并发计算最多只有一份变更提交成功。"""
    async with sessions() as session:
        row = await session.get(MemoryMaintenanceRow, turn_id)
        if row is None or row.owner_id != actor:
            raise ChatNotFoundError("记忆维护记录不可访问")
        if row.status in ("DONE", "DISABLED"):
            return dict(status=row.status, result=row.result)
        candidate = row.candidate
    try:
        async with asyncio.timeout(timeout_seconds):
            async with sessions() as session:
                enabled = (await settings(session, actor)).auto_write_enabled
                index = await catalogue(session, actor) if enabled else []
            # 维护只读模型实际请求的正文，更新只能针对本次读取版本。
            seen = {}
            async def read(note_id):
                async with sessions() as session:
                    note = await read_note(session, actor, note_id)
                seen[note_id] = note
                return note
            change = await agent.maintain(candidate, index=index, read_memory=read) if enabled else None
            async with sessions.begin() as session:
                await _lock(session, actor)
                saved = await session.get(MemoryMaintenanceRow, turn_id, populate_existing=True)
                if saved.status in ("DONE", "DISABLED"):
                    return dict(status=saved.status, result=saved.result)
                if not (await settings(session, actor)).auto_write_enabled:
                    saved.status, saved.result = "DISABLED", None
                else:
                    change = NoteChange.model_validate(change)
                    if change.note_id is not None:
                        prior = seen.get(change.note_id)
                        if prior is None or prior.version != change.expected_version:
                            raise ChatConflictError("只能修改本次已读取的笔记版本")
                        if not prior.active and change.action == "UPDATE":
                            raise ChatConflictError("自动维护不能重新启用已停用笔记")
                    if change.action == "CREATE":
                        note = await save_note(session, actor, change.document, source_id=turn_id)
                    elif change.action in ("UPDATE", "DEACTIVATE"):
                        document = change.document or NoteContent(**seen[change.note_id].model_dump(include={"title","description","content"}))
                        note = await save_note(session, actor, document, source_id=turn_id,
                            note_id=change.note_id, expected_version=change.expected_version, active=change.action != "DEACTIVATE")
                    else:
                        note = None
                    saved.status = "DONE"
                    saved.result = canonical_json(note) if note else None
                return dict(status=saved.status, result=saved.result)
    except Exception:
        # 不保存供应商错误正文，避免泄漏提示词或凭证；失败不会撤销回答。
        async with sessions.begin() as session:
            await _lock(session, actor)
            saved = await session.get(MemoryMaintenanceRow, turn_id, populate_existing=True)
            if saved.status not in ("DONE", "DISABLED"):
                saved.status, saved.result = "FAILED", None
            return dict(status=saved.status, result=saved.result)
