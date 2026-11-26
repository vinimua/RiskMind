"""显式启用的真实模型验收；只发送合成数据，输出质量证据和实际用量。"""
import asyncio
import json
import os
from time import perf_counter

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from risk_platform.conversations import agent as inputs
from risk_platform.conversations.agent import ChatAgent
from risk_platform.conversations.context import history_messages
from risk_platform.core.token_budget import request_tokens


pytestmark = pytest.mark.skipif(os.environ.get("RUN_CONTEXT_QUALITY_LIVE") != "1",
    reason="真实模型验收需显式设置 RUN_CONTEXT_QUALITY_LIVE=1、DEEPSEEK_API_KEY、DEEPSEEK_MODEL")


def test_repeated_summary_preserves_constraints_and_recalls_exact_original():
    """关键词断言只是最低门槛，输出原摘要和回答供人工核查语义。"""
    key = os.environ.get("DEEPSEEK_API_KEY")
    model_name = os.environ.get("DEEPSEEK_MODEL")
    if not key or not model_name:
        pytest.fail("真实模型验收未配置密钥和模型，不用替身冒充")
    calls = []

    class Usage(BaseCallbackHandler):
        def on_llm_end(self, response, **kwargs):
            calls.append((response.llm_output or {}).get("token_usage", {}))

    model = ChatOpenAI(model=model_name, api_key=key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        timeout=60, max_retries=0, callbacks=[Usage()])
    agent = ChatAgent(model_name, tool_model=model)
    exact = "原始工单标识为 RM-4827-QX，逐字核对时必须回查，不凭摘要猜测。"
    turns = [dict(turn_id=f"T{i}", turn_number=i,
        user="讨论 credit-v3，仅分析，不启动调查。" + ("本轮讨论统计口径，尚未取得正式指标。" * 90),
        assistant="这些是方法讨论，没有执行查询或确认原因。") for i in range(1, 13)]
    turns[0]["user"] = "收入缺失率升高不能直接证明风险上升。" + turns[0]["user"]
    turns[1]["user"] = exact + turns[1]["user"]
    turns[2]["assistant"] = "一次渠道查询失败，未取得结果；待按渠道比较，不能写成已完成。"
    turns[6]["user"] = "纠正：时间范围以最近30天为准，废弃此前最近7天的说法。"
    reads, saved_reads = [], []

    async def read_history(first, last, budget, interaction, start):
        assert first == last == 2 and interaction is None and start == 0
        selected = [turn for turn in turns if first <= turn["turn_number"] <= last]
        # 回查完整原文，不重新执行任何业务工具，也不对正文截断。
        result = dict(from_turn=first, to_turn=last, turns=[dict(turn_number=t["turn_number"],
            user=t["user"], assistant=t["assistant"]) for t in selected])
        assert request_tokens([result]) <= budget
        reads.append((first, last))
        return result

    async def record_read(pair):
        saved_reads.append(pair)

    async def scenario():
        started = perf_counter()
        first = await agent.summarize(None, turns[:6])
        summary = await agent.summarize(first, turns[6:])
        question = ("只回答，不执行动作：当前时间范围、禁止下的结论、失败查询和下一步分别是什么？"
            "另外请调用 read_chat_history 核对第2轮原文，再逐字给出原始工单标识。")
        response = await agent.decide(question, history=[], summary=summary, memory_index=[],
            read_memory=None, tool_names=(), read_history=read_history, record_read=record_read)
        before = request_tokens(*inputs.entry_request(question, history=history_messages(turns),
            summary=None, memory_index=[], tool_names=(), history_reader=True))
        after = request_tokens(*inputs.entry_request(question, history=[], summary=summary,
            memory_index=[], tool_names=(), history_reader=True))
        report = dict(model=model_name, estimated_before=before, estimated_after=after,
            elapsed_seconds=round(perf_counter()-started, 3), model_calls=len(calls),
            actual_usage=calls, summary=summary, answer=response.answer, reads=reads)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        assert after < before
        assert reads and len(reads) == len(saved_reads)
        assert "RM-4827-QX" in response.answer
        assert "30" in response.answer or "三十" in response.answer
        assert "失败" in response.answer or "未取得" in response.answer
        assert "渠道" in response.answer
        assert any(word in response.answer for word in ("不能", "不代表", "不等于", "不足以"))

    asyncio.run(scenario())
