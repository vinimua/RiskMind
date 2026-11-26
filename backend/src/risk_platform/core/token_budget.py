"""DeepSeek 本地文本分词与预算检查；服务端完整请求用量仍以 usage 为准。"""

import json
import hashlib
from functools import cache
from pathlib import Path

from tokenizers import Tokenizer

# 用户提供的 deepseek_v4_tokenizer.zip 中 tokenizer.json 的 SHA-256。
# 文件随 Python 包发布，不依赖 Downloads 路径、网络或运行时远程代码。
TOKENIZER_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"


@cache
def _tokenizer() -> Tokenizer:
    data = Path(__file__).with_name("deepseek_v4_tokenizer.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        raise RuntimeError("离线 tokenizer 文件与固定版本不一致")
    tokenizer = Tokenizer.from_str(data.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


class ContextBudgetExceeded(ValueError):
    """必要输入无法放入预算；调用方应明确反馈，不能当作零命中。"""


def estimate_tokens(text: str) -> int:
    """保留现有预算调用入口，用本地分词替代字符规则。

    只数传入文本，不额外添加 BOS/EOS。消息模板、工具协议和服务端模型
    版本可能带来差异，因此不把序列化 JSON 的计数称为 API 精确计费用量。
    缺失或损坏时明确失败，不静默回退旧算法。
    """
    return len(_tokenizer().encode(text, add_special_tokens=False).ids)


def request_tokens(messages, tools=()) -> int:
    """按实际消息与完整工具声明估算；支持 SDK 字典及 LangChain 消息。"""
    return estimate_tokens(json.dumps({"messages": [
        m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in messages
    ], "tools": list(tools)}, ensure_ascii=False))


def project_read_messages(messages, *, max_tokens, source_fields, tools=(), keep_recent=1):
    """只移出已完成的旧只读正文，保留请求配对与真实定位；不改持久原文。"""
    projected = list(messages)
    reads = []
    requests = {}
    group = -1
    for index, message in enumerate(messages):
        role = message.get("role") if isinstance(message, dict) else message.type
        if role in {"assistant", "ai"}:
            group = index
            calls = message.get("tool_calls", []) if isinstance(message, dict) else message.tool_calls
            requests.update({c["id"]:c.get("function", {}).get("name") or c.get("name") for c in calls})
        if role != "tool":
            continue
        content = message.get("content") if isinstance(message, dict) else message.content
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or not any(k in value for k in ("content", "items", "turns")):
            continue
        # 只有存在实际读取工具的来源可移出；任意业务工具输出不能凭字段相似被删除。
        call_id = message.get("tool_call_id") if isinstance(message, dict) else message.tool_call_id
        name = requests.get(call_id)
        fields = source_fields.get(name)
        if not fields or not all(k in value for k in fields):
            continue
        reference = {k: v for k, v in value.items() if k not in {"content", "items", "turns"}}
        reads.append((index, reference, group))
    groups = list(dict.fromkeys(group for _, _, group in reads))
    protected = set(groups[-keep_recent:]) if keep_recent else set()
    for index, reference, group in reads:
        if group in protected:
            continue
        if request_tokens(projected, tools) <= max_tokens:
            break
        content = json.dumps(reference | dict(body_omitted=True,
            recovery="如需原文，使用原读取工具及参数补读；此处不代表资料为空"), ensure_ascii=False)
        old = projected[index]
        replacement = dict(old, content=content) if isinstance(old, dict) else old.model_copy(update={"content":content})
        if request_tokens([replacement]) < request_tokens([old]):
            projected[index] = replacement
    return projected


def paragraph_page(content, *, start, max_tokens):
    """按完整段落返回正文；单段过大明确失败，不静默截断。"""
    paragraphs = content.replace("\r\n", "\n").replace("\r", "\n").split("\n\n")
    if not 0 <= start < len(paragraphs):
        raise ValueError("正文段落范围不存在")
    selected = []
    for paragraph in paragraphs[start:]:
        if estimate_tokens("\n\n".join([*selected, paragraph])) > max_tokens:
            break
        selected.append(paragraph)
    if not selected:
        raise ContextBudgetExceeded("单个完整段落超过剩余预算，请缩小任务或拆分源材料")
    end = start + len(selected)
    return dict(content="\n\n".join(selected), start_paragraph=start,
                next_paragraph=end if end < len(paragraphs) else None, total_paragraphs=len(paragraphs))


def check_request_budget(messages, *, input_limit: int, context_window: int,
                         output_tokens: int, safety_tokens: int) -> int:
    """检查实际消息序列化内容，保留输出空间；超限不静默裁剪业务事实。"""
    if min(input_limit, context_window, output_tokens, safety_tokens) <= 0:
        raise ValueError("token 预算必须为正数")
    available = min(input_limit, context_window - output_tokens - safety_tokens)
    # JSON 包含角色、字段及消息边界；仍是估算，不声称精确还原服务端模板。
    estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
    if available <= 0 or estimated > available:
        raise ContextBudgetExceeded("模型请求超过可用 token 预算；未调用模型")
    return estimated
