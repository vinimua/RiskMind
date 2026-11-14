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


def check_request_budget(messages, *, input_limit: int, context_window: int,
                         output_tokens: int, safety_tokens: int) -> int:
    """检查实际消息序列化内容，保留输出空间；超限不静默裁剪业务事实。"""
    if min(input_limit, context_window, output_tokens, safety_tokens) <= 0:
        raise ValueError("token 预算必须为正数")
    available = min(input_limit, context_window - output_tokens - safety_tokens)
    # JSON 包含角色、字段及消息边界；仍是估算，不声称精确还原服务端模板。
    estimated = estimate_tokens(json.dumps(messages, ensure_ascii=False))
    if available <= 0 or estimated > available:
        raise ValueError("模型请求超过可用 token 预算；未调用模型")
    return estimated
