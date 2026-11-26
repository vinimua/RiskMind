"""入口工具的固定声明和模型输出校验；本阶段不执行工具或提供授权。"""
import json
from types import MappingProxyType
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

from .contracts import ChatDraft, ProgressReadArguments, StartInvestigationArguments, ToolInvocation

# 与调查任务工具分开：这里调用的是案件业务入口，不伪造 InvestigationTask。
_ENTRY_TOOLS = MappingProxyType({
    "get_investigation_progress": (
        ProgressReadArguments,
        "查询一个明确案件的当前进度。只填写必需的 investigation_id。"
        "实时案件状态必须通过该工具读取，不能凭记忆回答；需要当前 READ 权限。",
    ),
    "start_investigation": (
        StartInvestigationArguments,
        "用户明确要求对指定模型启动新调查时提出请求。只填写必需的 model_id。"
        "咨询、否定、假设或目标不明时不得调用。"
        "这只是建议，服务器须取得用户显式确认并鉴权后才执行；不能表示已创建案件。",
    ),
})
ENTRY_TOOL_NAMES = tuple(_ENTRY_TOOLS)


def entry_tool_declarations(names=ENTRY_TOOL_NAMES):
    """调用方选择本次提供的子集；不接受任意工具 schema 或动态导入。"""
    names = tuple(names)
    if len(set(names)) != len(names) or any(name not in _ENTRY_TOOLS for name in names):
        raise ValueError("入口工具未注册或重复")
    declarations = []
    for name in names:
        declaration = convert_to_openai_tool(_ENTRY_TOOLS[name][0])
        declaration["function"].update(name=name, description=_ENTRY_TOOLS[name][1])
        declarations.append(declaration)
    return declarations


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


def parse_entry_decision(message: AIMessage, *, offered_names):
    """只接受完整回答或一个原生 function call；绝不从正文执行工具。"""
    offered_names = tuple(offered_names)
    entry_tool_declarations(offered_names)  # 验证本次提供列表本身。
    finish_reason = message.response_metadata.get("finish_reason")
    if message.additional_kwargs.get("refusal") or message.invalid_tool_calls:
        raise ValueError("模型没有可用决策")
    calls, content = message.tool_calls, message.content
    if calls:
        if finish_reason != "tool_calls" or len(calls) != 1:
            raise ValueError("工具响应不完整或包含多个调用")
        if content is not None and (not isinstance(content, str) or content.strip()):
            raise ValueError("不能同时输出正文和工具调用")
        call = calls[0]
        name = call["name"]
        if name not in offered_names:
            raise ValueError("工具未提供或不是 function call")
        arguments = _ENTRY_TOOLS[name][0].model_validate(call["args"])
        return ToolInvocation(call_id=call["id"], name=name, arguments=arguments)
    if finish_reason != "stop":
        raise ValueError("模型回答未正常结束")
    draft = ChatDraft.model_validate(_json_object(content))
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
