"""身份和业务依赖由部署组合入口提供；未配置时拒绝服务。"""
import inspect
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from risk_platform.memory.contracts import Identity
from risk_platform.entrypoints import AlertRequest
from risk_platform.conversations.contracts import ChatRequest
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

@router.post("/investigations/from-alert")
async def from_alert(body: AlertRequest, request: Request, actor: str = Depends(principal)):
    result = await invoke(request.app.state.entry_services.from_alert(body, actor=actor))
    return JSONResponse(content=jsonable_encoder(result))

@router.post("/chat/messages")
async def chat(body: ChatRequest, request: Request, actor: str = Depends(principal)):
    response = await invoke(request.app.state.entry_services.chat(body, actor=actor))
    status = 200 if response.status == "SUCCEEDED" else {
        "FORBIDDEN": 403, "NOT_FOUND": 404, "CONFLICT": 409}.get(response.error_code, 503)
    return JSONResponse(status_code=status, content=response.model_dump(mode="json"))
