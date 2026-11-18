"""注册 StructuredTool 及其结果协议；模型绑定与实际执行共用同一工具。

不动态导入、不自行调度、不自动重试；注册不等于本次运行绑定或授权。
"""

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from collections.abc import Callable

from pydantic import BaseModel
from langchain_core.tools import StructuredTool
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from risk_platform.execution import knowledge, csv_statistics
from risk_platform.execution.contracts import CsvStatisticsRequest, CsvStatisticsPayload, CsvStatisticsReference
from risk_platform.execution.contracts import KnowledgeSearchToolRequest, KnowledgeExecutionPayload, TaskExecutionResult
from risk_platform.workflow.contracts import CapabilityType
from risk_platform.workflow.runtime import InvestigationAction, InvestigationRuntimeContext, check_runtime


async def knowledge_search(config: RunnableConfig, **arguments):
    # RunnableConfig 由执行节点注入，身份和客户端不出现在模型参数中。
    runtime_context = config["configurable"]["runtime"]
    task = config["configurable"]["task"]
    check_runtime(runtime_context, InvestigationAction.CONTINUE)
    request = KnowledgeSearchToolRequest.model_validate(arguments)
    _knowledge_validate_request(runtime_context, request)
    # 只有实际进入这个工具适配器，才获取它的范围与独立授权函数。
    from risk_platform.workflow.knowledge_tool import knowledge_tool
    binding = knowledge_tool(runtime_context)
    trusted = binding.execution_context(runtime_context.timeout_seconds)
    result = await knowledge.execute_knowledge_search(task, request, context=trusted,
        authorize=binding.authorizer(runtime_context), session_factory=binding.session_factory,
        embedding_client=binding.embedding_client, milvus_client=binding.milvus_client)
    return TaskExecutionResult.model_validate(result.model_dump() | {
        "tool_name": "knowledge_search", "result_version": "1"})


def _knowledge_execution_limits(runtime):
    from risk_platform.workflow.knowledge_tool import knowledge_tool
    trusted = knowledge_tool(runtime).execution
    return {"max_top_k": trusted.max_top_k, "scope": trusted.scope.model_dump(mode="json"),
            "index_profile": trusted.index_profile.value}


def _knowledge_validate_request(runtime, request):
    from risk_platform.workflow.knowledge_tool import knowledge_tool
    if request.top_k > knowledge_tool(runtime).execution.max_top_k:
        raise ValueError("工具请求数量超过本次允许上限")


def _knowledge_authorize_result(runtime, result):
    from risk_platform.workflow.access import _check_execution
    from risk_platform.workflow.knowledge_tool import knowledge_execution
    _check_execution(result, knowledge_execution(runtime, InvestigationAction.READ))
    return True


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
    tool: StructuredTool
    capability: CapabilityType
    # 本次执行的公开范围与限制，随阶段输入固定；不包含客户端、凭证或权限函数。
    execution_limits: Callable[[InvestigationRuntimeContext], dict]
    validate_request: Callable[[InvestigationRuntimeContext, BaseModel], None]
    # 显式声明结果与引用协议，不默认套用知识检索结构。
    result_model: type[BaseModel]
    to_observation_items: Callable[[BaseModel], list[dict]]
    reference_model: type[BaseModel]
    validate_reference: Callable[[dict, BaseModel], None]
    # 仅检查工具数据权限；案件归属和运行权由业务授权层检查。
    authorize_result: Callable[[InvestigationRuntimeContext, TaskExecutionResult], bool]
    # 核验结果与原请求、执行限制的对应关系。
    validate_result: Callable[[BaseModel, dict, BaseModel], None]
    result_version: str = "1"

    def __post_init__(self):
        if not all(callable(function) for function in (
            self.execution_limits, self.validate_request,
            self.to_observation_items, self.validate_reference,
            self.authorize_result, self.validate_result,
        )):
            raise TypeError("工具执行、投影和校验字段必须是函数")
        if not all(isinstance(model, type) and issubclass(model, BaseModel) for model in (
            self.tool.args_schema, self.result_model, self.reference_model,
        )):
            raise TypeError("工具参数、结果和引用协议必须是 Pydantic 模型类")
        if self.tool.name != "knowledge_search" and "reference_id" not in self.reference_model.model_fields:
            raise ValueError("通用引用协议必须包含结果项 reference_id")

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
# - 参数已由 LangChain 解析为字典，符合该工具的请求模型。
# validate_tool_call() 再结合当前 runtime 检查：
# 当前运行权限与绑定
# → 工具仍然可用
# → 参数结构
# → 参数限制，例如 top_k 上限
# 没有把普通模型正文当成工具调用，也没有 query = task.objective 的兜底。

KNOWLEDGE_SEARCH = ToolDefinition(
    capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
    tool=StructuredTool.from_function(
        coroutine=knowledge_search, name="knowledge_search",
        args_schema=KnowledgeSearchToolRequest, infer_schema=False,
        description="检索带来源的历史案例和模型背景，返回知识线索；query、top_k 必填，filters 可选。"
                    "只填写这些检索参数；不能确认当前案件根因，也不能执行写操作。",
    ),
    execution_limits=_knowledge_execution_limits, validate_request=_knowledge_validate_request,
    result_model=KnowledgeExecutionPayload,
    to_observation_items=knowledge.observation_items,
    authorize_result=_knowledge_authorize_result,
    validate_reference=knowledge.validate_reference,
    reference_model=knowledge.KnowledgeReference,
    validate_result=knowledge.validate_result,
)
CSV_SAMPLE_STATISTICS = ToolDefinition(
    capability=CapabilityType.COMPUTATION,
    tool=StructuredTool.from_function(
        coroutine=csv_statistics.sample_statistics, name="csv_sample_statistics",
        args_schema=CsvStatisticsRequest, infer_schema=False,
        description="只读授权的离线 CSV，按左闭右开时间窗口统计申请数、坏标签占比或字段缺失率。"
                    "dataset_id、statistic、window_start、window_end 必填；仅 MISSING_RATE 必须填写 field。"
                    "不填写 query、top_k、filters 或案件入口参数。"
                    "不计算模型 KS/AUC，不证明标签成熟、线上坏账率或异常根因。",
    ),
    execution_limits=csv_statistics.execution_limits, validate_request=csv_statistics.validate_request,
    result_model=CsvStatisticsPayload, to_observation_items=csv_statistics.observation_items,
    reference_model=CsvStatisticsReference, validate_reference=csv_statistics.validate_reference,
    authorize_result=csv_statistics.authorize_result, validate_result=csv_statistics.validate_result,
)
TOOL_DEFINITIONS = MappingProxyType({tool.tool.name: tool for tool in (KNOWLEDGE_SEARCH, CSV_SAMPLE_STATISTICS)})


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
    available = tuple(tool for tool in matches if tool.tool.name in bound)
    if not available:
        raise ToolUnavailableError("TOOL_NOT_BOUND")
    return available


def validate_tool_call(call: ToolCall, capability: CapabilityType, runtime: InvestigationRuntimeContext) -> ToolCall:
    check_runtime(runtime, InvestigationAction.CONTINUE)
    available = {tool.tool.name: tool for tool in available_tools(capability, runtime)}
    if not isinstance(call, ToolCall) or not isinstance(call.tool_name, str) or call.tool_name not in available:
        raise ValueError("选择的工具未注册、能力不符或本次未绑定")
    definition = available[call.tool_name]
    arguments = call.arguments.model_dump(mode="json") if isinstance(call.arguments, BaseModel) else call.arguments
    request = definition.tool.args_schema.model_validate(arguments)
    definition.validate_request(runtime, request)
    return ToolCall(call.tool_name, request)
# 它返回校验后的工具名和参数，不执行工具，也不保存数据库。


def parse_tool_choice(message: AIMessage, capability: CapabilityType, *, allowed_names) -> ToolCall:
    """读取 LangChain 的标准调用，交给所选工具校验业务参数。"""
    if message.response_metadata.get("finish_reason") != "tool_calls" or message.invalid_tool_calls:
        raise ValueError("工具选择未完整返回原生调用")
    if len(message.tool_calls) != 1:
        raise ValueError("每个任务必须且只能提出一次工具调用")
    call = message.tool_calls[0]
    definition = tool_definition(call["name"])
    if definition.capability != capability or call["name"] not in allowed_names:
        raise ValueError("工具与正式任务能力或本次绑定不匹配")
    return ToolCall(call["name"], definition.tool.args_schema.model_validate(call["args"]))


def required_tool_options() -> dict:
    """DeepSeek thinking mode rejects required tool selection; keep it mandatory.

    Shared by the actual request and its persisted protocol digest.
    """
    return {"tool_choice": "required", "parallel_tool_calls": False,
            "extra_body": {"thinking": {"type": "disabled"}}}


def query_contract_digest(prompt: str, *, capability=CapabilityType.KNOWLEDGE_RETRIEVAL, names=None) -> str:
    """工具声明和调用协议也是模型输入的一部分，必须参与恢复规格摘要。"""
    payload = {"protocol": "langchain-structured-tool/v6", "system_prompt": prompt,
               "tools": [convert_to_openai_tool(d.tool) for d in TOOL_DEFINITIONS.values()
                         if d.capability == capability and (names is None or d.tool.name in names)],
               **required_tool_options()}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
