"""显式工具白名单：声明给模型看，执行仍进入已有受控适配器。

不动态导入、不自行调度、不自动重试。当前只登记 knowledge_search。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json
from types import MappingProxyType
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from risk_platform.execution import knowledge
from risk_platform.execution.contracts import ExecutionError, KnowledgeSearchToolRequest, TaskExecutionResult
from risk_platform.workflow.contracts import CapabilityType


async def _knowledge_handler(task, request, **options):
    return await knowledge.execute_knowledge_search(task, request, **options)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    capability: CapabilityType
    description: str
    request_model: type[BaseModel]
    handler: Callable[..., Awaitable[TaskExecutionResult]]

    def declaration(self) -> dict:
        # 不把 actor、scope、profile、策略函数或 handler 暴露给模型。
        # 不依赖供应商 strict JSON Schema 支持；本地仍必须完整校验。
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.request_model.model_json_schema(), "strict": False,
        }}


KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search", capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
    description="检索带来源的历史案例和模型背景，返回知识线索；不能确认当前案件根因，也不能执行写操作。",
    request_model=KnowledgeSearchToolRequest, handler=_knowledge_handler,
)
TOOL_DEFINITIONS = MappingProxyType({KNOWLEDGE_SEARCH.name: KNOWLEDGE_SEARCH})


def tool_declarations(capability: CapabilityType) -> list[dict]:
    declarations = [tool.declaration() for tool in TOOL_DEFINITIONS.values() if tool.capability == capability]
    if not declarations:
        raise ValueError("当前任务没有已注册的可用工具")
    return declarations


def parse_tool_choice(choice, capability: CapabilityType) -> KnowledgeSearchToolRequest:
    """只接受一次原生 function call；不解析普通正文，也不回退到 objective。"""
    if getattr(choice, "finish_reason", None) != "tool_calls":
        raise ValueError("工具选择未完整返回原生调用")
    calls = getattr(choice.message, "tool_calls", None)
    if not isinstance(calls, list) or len(calls) != 1:
        raise ValueError("每个任务必须且只能提出一次工具调用")
    call = calls[0]
    if getattr(call, "type", None) != "function":
        raise ValueError("只接受已注册的 function 工具")
    function = getattr(call, "function", None)
    name = getattr(function, "name", None)
    if not isinstance(name, str) or name not in TOOL_DEFINITIONS:
        raise ValueError("模型选择了未注册工具")
    definition = TOOL_DEFINITIONS[name]
    if definition.capability != capability:
        raise ValueError("工具与正式任务能力不匹配")
    arguments = getattr(function, "arguments", None)
    if not isinstance(arguments, str):
        raise ValueError("工具参数必须为 JSON 字符串")
    # 当前唯一知识工具与账本 Literal[knowledge_search] 一一对应，校验后无损投影为旧请求契约。
    # 增加第二个工具前必须演进查询产物，不能继续丢弃工具选择信息。
    if name != KNOWLEDGE_SEARCH.name:
        raise ValueError("当前持久化协议尚未支持该工具")
    return definition.request_model.model_validate_json(arguments)


async def execute_registered_tool(name: str, task, request, **options) -> TaskExecutionResult:
    """白名单分派不是授权；handler 内仍做专用参数、scope、权限和结果校验。"""
    code = None
    if not isinstance(name, str) or name not in TOOL_DEFINITIONS:
        code = "UNKNOWN_TOOL"
    elif TOOL_DEFINITIONS[name].capability != task.capability:
        code = "UNSUPPORTED_CAPABILITY"
    if code is not None:
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="exec-" + uuid4().hex, task_id=task.task_id,
            capability=task.capability, status="REJECTED", started_at=now, completed_at=now,
            error=ExecutionError(category="VALIDATION", code=code,
                message="工具未注册或不支持当前任务能力", retryable=False))
    return await TOOL_DEFINITIONS[name].handler(task, request, **options)


def query_contract_digest(prompt: str) -> str:
    """工具声明和调用协议也是模型输入的一部分，必须参与恢复规格摘要。"""
    payload = {"protocol": "knowledge-function-call/v1", "system_prompt": prompt,
               "tools": tool_declarations(CapabilityType.KNOWLEDGE_RETRIEVAL),
               "tool_choice": "required", "parallel_tool_calls": False}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
