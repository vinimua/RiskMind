"""入口工具的固定声明和模型输出校验；本阶段不执行工具或提供授权。"""
import json
from types import MappingProxyType

from .contracts import ChatDraft, ProgressReadArguments, StartInvestigationArguments, ToolInvocation

# 与调查任务工具分开：这里调用的是案件业务入口，不伪造 InvestigationTask。
_ENTRY_TOOLS = MappingProxyType({
    "get_investigation_progress": (
        ProgressReadArguments,
        "查询一个明确案件的当前进度。实时案件状态必须通过该工具读取，不能凭记忆回答；需要当前 READ 权限。",
    ),
    "start_investigation": (
        StartInvestigationArguments,
        "用户明确要求对指定模型启动新调查时提出请求。咨询、否定、假设或目标不明时不得调用。"
        "这只是建议，服务器须取得用户显式确认并鉴权后才执行；不能表示已创建案件。",
    ),
})
ENTRY_TOOL_NAMES = tuple(_ENTRY_TOOLS)


def entry_tool_declarations(names=ENTRY_TOOL_NAMES):
    """调用方选择本次提供的子集；不接受任意工具 schema 或动态导入。"""
    names = tuple(names)
    if len(set(names)) != len(names) or any(name not in _ENTRY_TOOLS for name in names):
        raise ValueError("入口工具未注册或重复")
    return [{"type": "function", "function": {
        # DeepSeek server-side strict requires /beta; standard endpoint relies on
        # the full local parser/target gate below, as investigation tools already do.
        "name": name, "description": _ENTRY_TOOLS[name][1], "strict": False,
        "parameters": _ENTRY_TOOLS[name][0].model_json_schema(),
    }} for name in names]


def _json_object(text):
    """拒绝重复字段和非 JSON 数字，避免同一输入产生多种解释。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("模型 JSON 含重复字段")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("模型 JSON 含非法数字")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型未返回完整 JSON")
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("模型 JSON 必须是对象")
    return value


def parse_entry_decision(response, *, offered_names):
    """只接受完整回答或一个原生 function call；绝不从正文执行工具。"""
    offered_names = tuple(offered_names)
    entry_tool_declarations(offered_names)  # 验证本次提供列表本身。
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise ValueError("模型必须返回一个完整选择")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    message = getattr(choice, "message", None)
    if message is None or getattr(message, "refusal", None):
        raise ValueError("模型没有可用决策")
    # 不支持旧 function_call 字段或非原生调用格式。
    if getattr(message, "function_call", None) is not None:
        raise ValueError("不接受旧 function_call 响应")
    calls = getattr(message, "tool_calls", None)
    if calls is None:
        calls = []
    if not isinstance(calls, (list, tuple)):
        raise ValueError("tool_calls 必须是原生调用列表")
    content = getattr(message, "content", None)
    if calls:
        if finish_reason != "tool_calls" or len(calls) != 1:
            raise ValueError("工具响应不完整或包含多个调用")
        if content is not None and (not isinstance(content, str) or content.strip()):
            raise ValueError("不能同时输出正文和工具调用")
        call = calls[0]
        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        if getattr(call, "type", None) != "function" or name not in offered_names:
            raise ValueError("工具未提供或不是 function call")
        arguments = _ENTRY_TOOLS[name][0].model_validate(_json_object(getattr(function, "arguments", None)))
        return ToolInvocation(call_id=getattr(call, "id", None), name=name, arguments=arguments)
    if finish_reason != "stop":
        raise ValueError("模型回答未正常结束")
    draft = ChatDraft.model_validate(_json_object(content))
    if draft.memory.execution_summaries:
        raise ValueError("聊天回答不能生成正式执行事实")
    return draft


class EntryTargetError(ValueError):
    """只包含可向用户展示的追问；不把目标合法性当作业务授权。"""


def validate_entry_target(request, invocation):
    """优先使用明确绑定；否则要求目标作为独立 ID 出现在本条消息，不从记忆补造。"""
    import re
    invocation = ToolInvocation.model_validate_json(invocation.model_dump_json())
    reading = invocation.name == "get_investigation_progress"
    target = invocation.arguments.investigation_id if reading else invocation.arguments.model_id
    explicit = request.investigation_id if reading else request.model_id
    opposite = request.model_id if reading else request.investigation_id
    if opposite or (explicit is not None and explicit != target):
        raise EntryTargetError("请求绑定的对象与工具目标冲突，请明确案件或模型目标。")
    if explicit is None and re.search(r"(?<![A-Za-z0-9_.:-])" + re.escape(target) + r"(?![A-Za-z0-9_.:-])", request.message) is None:
        raise EntryTargetError("请明确提供要查询的案件 ID。" if reading else "请明确提供要启动调查的模型 ID。")
    return invocation
