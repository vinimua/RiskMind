"""调用前的近似 token 检查；不是供应商 tokenizer 或计费结果。"""

import json
import re


def estimate_tokens(text: str) -> int:
    """参考 DevF：汉字及符号逐个计数，ASCII 单词按约四字符一 token。

    非 ASCII 字符也计入，避免中文以外的文本被正则漏掉。误差由调用方
    配置的安全余量缓冲；最终真实用量仍以供应商 usage 为准。
    """
    parts = re.findall(r"[A-Za-z0-9_-]+|[^\s]", text)
    return sum(max(1, (len(part) + 3) // 4) if part.isascii() and
               re.fullmatch(r"[A-Za-z0-9_-]+", part) else 1 for part in parts)


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
