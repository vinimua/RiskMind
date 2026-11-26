"""身份和业务依赖由部署组合入口提供；未配置时拒绝服务。"""
import inspect
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pathlib import Path
from pydantic import TypeAdapter
from risk_platform.memory.contracts import Identity
from risk_platform.entrypoints import AlertRequest
from risk_platform.conversations.contracts import ChatRequest, EntryConfirmation
from risk_platform.conversations.repository import ChatConflictError, ChatBusyError, ChatNotFoundError
from risk_platform.workflow.persistence.repository import LedgerConflictError, LedgerIntegrityError
from risk_platform.workflow.run_guard import RecoveryBlockedError
from risk_platform.execution.actions import ActionDecision, ManualResolution
from risk_platform.memory import personal
from risk_platform.memory.personal_contracts import NoteContent, NoteCreate, NoteUpdate, MemorySettings
from risk_platform.conversations.repository import stable_id

router = APIRouter()


@router.get("/action-review", include_in_schema=False)
async def action_review():
    return Response((Path(__file__).parents[1] / "action_review.html").read_text(encoding="utf-8"),
        media_type="text/html", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def action_response(result):
    return JSONResponse(status_code=202 if result.get("status") in {"WAITING_APPROVAL", "WAITING_VERIFICATION"} else 200,
        content=jsonable_encoder(result))


async def principal(request: Request):
    authenticate = request.app.state.authenticate
    if authenticate is None or request.app.state.entry_services is None:
        raise HTTPException(503, "入口尚未配置身份认证和业务依赖")
    try:
        actor = authenticate(request)
        if inspect.isawaitable(actor):
            actor = await actor
        return TypeAdapter(Identity).validate_python(actor)
    except Exception:
        raise HTTPException(401, "身份认证失败") from None


@router.get("/memory/settings")
async def memory_settings(request: Request, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions() as session:
        return await personal.settings(session, actor)


@router.put("/memory/settings")
async def update_memory_settings(body: MemorySettings, request: Request, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions.begin() as session:
        return await personal.set_settings(session, actor, body)


@router.get("/memory/notes")
async def memory_notes(request: Request, query: str | None = None, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions() as session:
        return await personal.catalogue(session, actor, query=query)


@router.get("/memory/notes/{note_id}")
async def memory_note(note_id: str, request: Request, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions() as session:
        return await invoke(personal.read_note(session, actor, note_id))


@router.post("/memory/notes")
async def create_memory_note(body: NoteCreate, request: Request, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions.begin() as session:
        return await invoke(personal.save_note(session, actor, NoteContent(**body.model_dump(exclude={"request_id"})),
            source_id=stable_id("manual-", actor, body.request_id)))


@router.get("/memory/notes/{note_id}/revisions")
async def memory_revisions(note_id: str, request: Request, actor: str = Depends(principal)):
    from sqlalchemy import select
    from risk_platform.memory.models import PersonalNoteRevisionRow
    async with request.app.state.entry_services.sessions() as session:
        await invoke(personal.read_note(session, actor, note_id))
        rows = await session.scalars(select(PersonalNoteRevisionRow).where(
            PersonalNoteRevisionRow.note_id == note_id).order_by(PersonalNoteRevisionRow.version))
        return [dict(version=row.version, source_id=row.source_id, document=row.payload) for row in rows]


@router.put("/memory/notes/{note_id}")
async def update_memory_note(note_id: str, body: NoteUpdate, request: Request, actor: str = Depends(principal)):
    async with request.app.state.entry_services.sessions.begin() as session:
        return await invoke(personal.save_note(session, actor, NoteContent(**body.model_dump(include={"title","description","content"})),
            source_id=stable_id("manual-", actor, body.request_id), note_id=note_id,
            expected_version=body.expected_version, active=body.active))


@router.get("/memory/maintenance/{turn_id}")
async def memory_maintenance_status(turn_id: str, request: Request, actor: str = Depends(principal)):
    from risk_platform.memory.models import MemoryMaintenanceRow
    async with request.app.state.entry_services.sessions() as session:
        row = await session.get(MemoryMaintenanceRow, turn_id)
        if row is None or row.owner_id != actor:
            raise HTTPException(404, "维护记录不可访问")
        return dict(status=row.status, result=row.result)


@router.post("/memory/maintenance/{turn_id}/retry")
async def retry_memory_maintenance(turn_id: str, request: Request, actor: str = Depends(principal)):
    services = request.app.state.entry_services
    return await invoke(personal.maintenance(services.sessions, actor, turn_id, services.chat_agent,
                                             timeout_seconds=min(10, services.timeout_seconds)))

async def invoke(operation):
    try:
        return await operation
    except PermissionError:
        raise HTTPException(403, "无操作权限") from None
    except ChatNotFoundError:
        raise HTTPException(404, "资源不可访问") from None
    except (ChatConflictError, ChatBusyError, LedgerConflictError, RecoveryBlockedError):
        raise HTTPException(409, "请求冲突或运行被阻断") from None
    except LedgerIntegrityError:
        raise HTTPException(500, "执行记录校验失败") from None
    except Exception:
        raise HTTPException(503, "依赖不可用或执行未完成") from None
#异常告警入口
@router.post("/investigations/from-alert")
async def from_alert(body: AlertRequest, request: Request, actor: str = Depends(principal)):
    result = await invoke(request.app.state.entry_services.from_alert(body, actor=actor))
    return action_response(result)
def entry_response(view):
    """等待确认或处理中返回 202，失败返回对应错误码。"""
    status = 202
    if view.result is not None:
        status = 200 if view.result.status == "SUCCEEDED" else {
            "FORBIDDEN": 403, "NOT_FOUND": 404, "CONFLICT": 409, "CONTEXT_BUDGET": 413}.get(view.result.error_code, 503)
    return JSONResponse(status_code=status, content=view.model_dump(mode="json"))

# Depends(principal) 表示：FastAPI 先执行 principal()，把它返回的用户身份传给 actor，再执行 chat()。
@router.post("/chat/messages")
async def chat(body: ChatRequest, request: Request, actor: str = Depends(principal)):
    return entry_response(await invoke(request.app.state.entry_services.chat(body, actor=actor)))
# 当前是单用户本地模式：正确 Token 对应配置里的固定用户。actor 不是 Token，也不是 Agent，而是“这次请求由谁发起”的身份。
# “单用户本地模式”就是：当前启动的后端，只认配置里的一个用户和一个 Token。

# | 编号 | 标识什么 |
# |---|---|
# | `conversation_id` | 哪一段会话 |
# | `turn_id` | 会话里的哪一次请求 |
# | `invocation_id` | 这次请求提出的哪一个工具调用 |
# | `investigation_id` | 哪一个调查案件 |

# 当前 turn_id 由服务端根据 用户身份 + 客户端 request_id 稳定生成。
# 同一次请求重发时保持 request_id 不变，就会对应同一个 turn_id；用户发送新消息则使用新的 request_id。

# 因为 request_id 标识一次请求，conversation_id 才标识整段会话。
# 规则是：
# - 新消息：新 request_id，继续使用同一个 conversation_id。
# - 网络失败，重发原消息：保持原 request_id 和内容不变，避免重复执行。
# - 确认启动：使用原 turn_id，确认对应的建议
@router.post("/chat/turns/{turn_id}/confirmation")
async def confirm_chat(turn_id: str, body: EntryConfirmation, request: Request, actor: str = Depends(principal)):
    return entry_response(await invoke(request.app.state.entry_services.confirm_chat(turn_id, body, actor=actor)))


@router.get("/chat/turns/{turn_id}")
async def inspect_chat(turn_id: str, request: Request, actor: str = Depends(principal)):
    return entry_response(await invoke(request.app.state.entry_services.inspect_entry(turn_id, actor=actor)))


@router.get("/chat/conversations/{conversation_id}/history")
async def conversation_history(conversation_id: str, request: Request,
                               after_turn: int = Query(default=0, ge=0),
                               limit: int = Query(default=100, ge=1, le=100), actor: str = Depends(principal)):
    from risk_platform.conversations import repository as chat_store
    services = request.app.state.entry_services
    chat_store.authorize(services.authorize_conversation, actor, "chat:read", conversation_id)
    async with services.sessions() as session:
        # 返回普通聊天原文；案件工具结果仍须通过案件授权接口读取。
        return await invoke(chat_store.history_turns(session, conversation_id, actor, after_turn=after_turn, limit=limit))


@router.get("/actions/{action_id}")
async def inspect_action(action_id: str, request: Request, actor: str = Depends(principal)):
    return action_response(await invoke(request.app.state.entry_services.inspect_action(action_id, actor=actor)))


@router.post("/actions/{action_id}/confirmation")
async def confirm_action(action_id: str, body: ActionDecision, request: Request, actor: str = Depends(principal)):
    return action_response(await invoke(request.app.state.entry_services.confirm_action(action_id, body, actor=actor)))


@router.post("/actions/{action_id}/resolution")
async def resolve_action(action_id: str, body: ManualResolution, request: Request, actor: str = Depends(principal)):
    return action_response(await invoke(request.app.state.entry_services.resolve_action(action_id, body, actor=actor)))


@router.post("/investigations/{case_id}/resume")
async def resume_investigation(case_id: str, request: Request, actor: str = Depends(principal)):
    return action_response(await invoke(request.app.state.entry_services.resume_investigation(case_id, actor=actor)))


@router.get("/actions/{action_id}/report")
async def download_report(action_id: str, request: Request, actor: str = Depends(principal)):
    content = await invoke(request.app.state.entry_services.download_report(action_id, actor=actor))
    return Response(content=content, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="investigation-report.md"', "Cache-Control": "no-store"})
