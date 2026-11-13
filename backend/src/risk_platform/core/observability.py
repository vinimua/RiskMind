"""可选 Langfuse 观测：仅发送允许的结构化属性，不发送业务正文或原始异常。"""
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from threading import Lock

logger = logging.getLogger(__name__)
_client = None
_client_lock = Lock()
_current = ContextVar("risk_observation", default=None)


def get_langfuse():
    global _client
    if os.getenv("RISK_LANGFUSE_ENABLED", "false").lower() != "true":
        return None
    if not all(os.getenv(key) for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")):
        return None
    try:
        with _client_lock:
            if _client is None:
                from langfuse import Langfuse
                _client = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                    secret_key=os.environ["LANGFUSE_SECRET_KEY"], base_url=os.environ["LANGFUSE_BASE_URL"],
                    timeout=2)
            return _client
    except Exception:
        logger.warning("Langfuse initialization failed; observability disabled for this call")
        return None


def _update(observation, **values):
    if observation is not None:
        try:
            observation.update(**values)
        except Exception:
            logger.warning("Langfuse observation update failed; ignored")


@contextmanager
def observation(name, *, model=None, metadata=None):
    """SDK 进入/更新/退出失败不影响业务；原始业务异常绝不传给 SDK 上下文。"""
    manager = span = None
    try:
        client = get_langfuse()
        if client is not None:
            options = dict(name=name, as_type="generation" if model else "span", metadata=metadata or {})
            if model:
                options["model"] = model
            manager = client.start_as_current_observation(**options)
            span = manager.__enter__()
    except Exception:
        manager = None
        logger.warning("Langfuse observation creation failed; ignored")
    token = _current.set(span)
    try:
        yield span
    except BaseException:
        # 不包含异常文本、请求正文、模型响应或 traceback，防止导出凭证/个人信息。
        _update(span, level="ERROR", status_message="operation_failed")
        raise
    finally:
        _current.reset(token)
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                logger.warning("Langfuse observation finalization failed; ignored")


def trace_chat(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with observation("chat-request") as span:
            result = await function(*args, **kwargs)
            response = getattr(result, "result", None)
            error_code = getattr(result, "error_code", None) or getattr(response, "error_code", None)
            _update(span, metadata={"status": result.status, "error_code": error_code or "NONE"},
                    level="ERROR" if error_code or result.status in ("FAILED", "UNCERTAIN") else "DEFAULT")
            return result
    return wrapped


def observe_generation(name):
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            with observation(name, model=self.model) as span:
                result = function(self, *args, **kwargs)
                if name == "entry-decision":
                    # 只记录注册工具名称/无工具，不导出正文、参数或目标身份。
                    _update(span, metadata={"selected_tool": getattr(result, "name", "NONE")})
                return result
        return wrapped
    return decorate


def record_usage(response):
    """仅接受供应商实际返回的非负整数 token 用量；缺失不伪造为 0，不猜价格。"""
    try:
        usage = getattr(response, "usage", None)
        details = {}
        for source, target in (("prompt_tokens", "input"), ("completion_tokens", "output"), ("total_tokens", "total")):
            count = getattr(usage, source, None)
            if type(count) is int and count >= 0:
                details[target] = count
        if details:
            _update(_current.get(), usage_details=details)
    except Exception:
        logger.warning("Langfuse usage recording failed; ignored")


def tag_chat_turn(conversation_id, turn_id, action, *, cached=False):
    # conversation/turn 均为本系统生成的哈希身份，不导出原 actor、request_id 或案件目标。
    _update(_current.get(), metadata={"conversation_id": conversation_id, "turn_id": turn_id,
                                     "action": action, "cached": cached})


def shutdown_observability():
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.shutdown()
        except Exception:
            logger.warning("Langfuse shutdown failed; ignored")
