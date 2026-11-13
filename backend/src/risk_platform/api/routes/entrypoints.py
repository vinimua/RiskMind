"""身份和业务依赖由部署组合入口提供；未配置时拒绝服务。"""
import inspect
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from risk_platform.memory.contracts import Identity
from risk_platform.entrypoints import AlertRequest
from risk_platform.conversations.contracts import ChatRequest, EntryConfirmation
from risk_platform.conversations.repository import ChatConflictError, ChatBusyError, ChatNotFoundError
from risk_platform.workflow.persistence.repository import LedgerConflictError, LedgerIntegrityError
from risk_platform.workflow.run_guard import RecoveryBlockedError

router = APIRouter()

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
    return JSONResponse(content=jsonable_encoder(result))
def entry_response(view):
    """等待确认或处理中返回 202，失败返回对应错误码。"""
    status = 202
    if view.result is not None:
        status = 200 if view.result.status == "SUCCEEDED" else {
            "FORBIDDEN": 403, "NOT_FOUND": 404, "CONFLICT": 409}.get(view.result.error_code, 503)
    return JSONResponse(status_code=status, content=view.model_dump(mode="json"))


@router.post("/chat/messages")
async def chat(body: ChatRequest, request: Request, actor: str = Depends(principal)):
    return entry_response(await invoke(request.app.state.entry_services.chat(body, actor=actor)))

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
