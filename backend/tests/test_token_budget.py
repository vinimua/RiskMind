"""离线分词与原有预算边界；不访问模型服务。"""

import json
from pathlib import Path
import socket

import pytest
from tokenizers import Tokenizer

from risk_platform.core import token_budget


@pytest.mark.parametrize("text", [
    "", " ", "Hello!", "中文标签成熟度", "English 与中文 mixed 123.45",
    "😀 café\nمرحبا", '{"query":"标签规范","top_k":5}',
    "<｜begin▁of▁sentence｜>", "完整材料，不应被截断。" * 3000,
], ids=["empty", "space", "english", "chinese", "mixed", "unicode", "json", "special", "long"])
def test_count_matches_supplied_tokenizer_without_network(monkeypatch, text):
    def forbidden(*args, **kwargs):
        pytest.fail("离线计数不能联网")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    path = Path(token_budget.__file__).with_name("deepseek_v4_tokenizer.json")
    reference = Tokenizer.from_file(str(path))
    reference.no_padding()
    reference.no_truncation()
    assert token_budget.estimate_tokens(text) == len(reference.encode(text, add_special_tokens=False).ids)


def test_budget_boundary_still_reserves_output_and_safety():
    messages = [{"role": "user", "content": "标签规范"}]
    count = token_budget.estimate_tokens(json.dumps(messages, ensure_ascii=False))
    assert token_budget.check_request_budget(messages, input_limit=count,
        context_window=count + 20, output_tokens=10, safety_tokens=10) == count
    with pytest.raises(ValueError, match="token 预算"):
        token_budget.check_request_budget(messages, input_limit=count - 1,
            context_window=count + 20, output_tokens=10, safety_tokens=10)


def test_tokenizer_is_loaded_once():
    token_budget._tokenizer.cache_clear()
    try:
        token_budget.estimate_tokens("first")
        token_budget.estimate_tokens("second")
        assert token_budget._tokenizer.cache_info().misses == 1
        assert token_budget._tokenizer.cache_info().hits == 1
    finally:
        token_budget._tokenizer.cache_clear()


def test_corrupt_local_file_does_not_fall_back(monkeypatch):
    token_budget._tokenizer.cache_clear()
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"{}")
    try:
        with pytest.raises(RuntimeError, match="tokenizer"):
            token_budget.estimate_tokens("hello")
    finally:
        token_budget._tokenizer.cache_clear()
