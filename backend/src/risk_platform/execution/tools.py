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
from risk_platform.workflow.persistence.contracts import OperationType
from risk_platform.workflow.runtime import InvestigationAction, InvestigationRuntimeContext, check_runtime


async def _knowledge_handler(task, request, *, runtime_context):
    # 只有实际进入这个工具适配器，才获取它的范围与独立授权函数。
    from risk_platform.workflow.knowledge_tool import knowledge_execution, knowledge_authorizer, knowledge_tool
    binding = knowledge_tool(runtime_context)
    trusted = knowledge_execution(runtime_context, InvestigationAction.CONTINUE)
    return await knowledge.execute_knowledge_search(task, request, context=trusted,
        authorize=knowledge_authorizer(runtime_context), session_factory=binding.session_factory,
        embedding_client=binding.embedding_client, milvus_client=binding.milvus_client)


def _knowledge_model_input(runtime):
    from risk_platform.workflow.knowledge_tool import knowledge_execution
    trusted = knowledge_execution(runtime, InvestigationAction.CONTINUE)
    return {"max_top_k": trusted.max_top_k}


def _knowledge_validate_request(runtime, request):
    from risk_platform.workflow.knowledge_tool import knowledge_execution
    if request.top_k > knowledge_execution(runtime, InvestigationAction.CONTINUE).max_top_k:
        raise ValueError("工具请求数量超过本次允许上限")


@dataclass(frozen=True)
class ToolCall:
    """内部选择结果；保留名称，参数随后按所选工具的专用模型校验。"""
    tool_name: str
    arguments: object


class ToolUnavailableError(PermissionError):
    def __init__(self, code):
        self.code = code
        super().__init__("当前任务没有已绑定的可用工具")


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    capability: CapabilityType
    description: str
    request_model: type[BaseModel]
    handler: Callable[..., Awaitable[TaskExecutionResult]]
    model_input: Callable[[InvestigationRuntimeContext], dict]
    validate_request: Callable[[InvestigationRuntimeContext, BaseModel], None]
    execution_phase: OperationType

    def declaration(self) -> dict:
        # 不把 actor、scope、profile、策略函数或 handler 暴露给模型。
        # 不依赖供应商 strict JSON Schema 支持；本地仍必须完整校验。
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.request_model.model_json_schema(), "strict": False,
        }}

# 模型选择后怎么校验
# 链路是：
# Agent.formulate_tool_call()
#     ↓
# parse_tool_choice()
#     ↓
# validate_tool_call()
# parse_tool_choice() 检查：
# - 返回的是完整的原生 tool_calls；
# - 必须且只能有一次 function call；
# - 工具已注册、能力匹配，并在本次提供列表中；
# - 参数是合法 JSON，符合该工具的请求模型。
# validate_tool_call() 再结合当前 runtime 检查：
# 当前运行权限与绑定
# → 工具仍然可用
# → 参数结构
# → 参数限制，例如 top_k 上限
# 没有把普通模型正文当成工具调用，也没有 query = task.objective 的兜底。

KNOWLEDGE_SEARCH = ToolDefinition(
    name="knowledge_search", capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
    description="检索带来源的历史案例和模型背景，返回知识线索；不能确认当前案件根因，也不能执行写操作。",
    request_model=KnowledgeSearchToolRequest, handler=_knowledge_handler,
    model_input=_knowledge_model_input, validate_request=_knowledge_validate_request,
    execution_phase=OperationType.KNOWLEDGE_EXECUTION,
)
TOOL_DEFINITIONS = MappingProxyType({KNOWLEDGE_SEARCH.name: KNOWLEDGE_SEARCH})


def tool_declarations(capability: CapabilityType, *, names=None) -> list[dict]:
    declarations = [tool.declaration() for tool in TOOL_DEFINITIONS.values() if tool.capability == capability and (names is None or tool.name in names)]
    if not declarations:
        raise ValueError("当前任务没有已注册的可用工具")
    return declarations


def tool_definition(name: str) -> ToolDefinition:
    if not isinstance(name, str) or name not in TOOL_DEFINITIONS:
        raise ValueError("工具未注册")
    return TOOL_DEFINITIONS[name]


def available_tools(capability: CapabilityType, runtime: InvestigationRuntimeContext) -> tuple[ToolDefinition, ...]:
    """取注册、任务能力、本次可信绑定的交集；不以注册代替授权。"""
    matches = [tool for tool in TOOL_DEFINITIONS.values() if tool.capability == capability]
    if not matches:
        raise ToolUnavailableError("UNSUPPORTED_CAPABILITY")
    bound = {tool.name for tool in runtime.tools}
    available = tuple(tool for tool in matches if tool.name in bound)
    if not available:
        raise ToolUnavailableError("TOOL_NOT_BOUND")
    return available


def validate_tool_call(call: ToolCall, capability: CapabilityType, runtime: InvestigationRuntimeContext) -> ToolCall:
    check_runtime(runtime, InvestigationAction.CONTINUE)
    available = {tool.name: tool for tool in available_tools(capability, runtime)}
    if not isinstance(call, ToolCall) or not isinstance(call.tool_name, str) or call.tool_name not in available:
        raise ValueError("选择的工具未注册、能力不符或本次未绑定")
    definition = available[call.tool_name]
    arguments = call.arguments.model_dump(mode="json") if isinstance(call.arguments, BaseModel) else call.arguments
    request = definition.request_model.model_validate(arguments)
    definition.validate_request(runtime, request)
    return ToolCall(call.tool_name, request)


def parse_tool_choice(choice, capability: CapabilityType, *, allowed_names=None) -> ToolCall:
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
    if definition.capability != capability or (allowed_names is not None and name not in allowed_names):
        raise ValueError("工具与正式任务能力或本次绑定不匹配")
    arguments = getattr(function, "arguments", None)
    if not isinstance(arguments, str):
        raise ValueError("工具参数必须为 JSON 字符串")
    return ToolCall(name, definition.request_model.model_validate_json(arguments))


async def execute_registered_tool(name: str, task, request, *, runtime_context) -> TaskExecutionResult:
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
    check_runtime(runtime_context, InvestigationAction.CONTINUE)
    if not any(tool.name == name for tool in runtime_context.tools):
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="exec-" + uuid4().hex, task_id=task.task_id,
            capability=task.capability, status="REJECTED", started_at=now, completed_at=now,
            error=ExecutionError(category="AUTHORIZATION", code="TOOL_NOT_BOUND",
                message="本次运行未绑定所选工具", retryable=False))
    return await TOOL_DEFINITIONS[name].handler(task, request, runtime_context=runtime_context)


def query_contract_digest(prompt: str) -> str:
    """工具声明和调用协议也是模型输入的一部分，必须参与恢复规格摘要。"""
    payload = {"protocol": "bound-tool-function-call/v2", "system_prompt": prompt,
               "tools": tool_declarations(CapabilityType.KNOWLEDGE_RETRIEVAL),
               "tool_choice": "required", "parallel_tool_calls": False}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
