"""先验证决策边界；此处不假装已经具备跨轮次持久化或图回边。"""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.core.token_budget import check_request_budget, estimate_tokens
from risk_platform.workflow.replanning import ReplanningInput
from risk_platform.workflow.observation import KnowledgeObservationDraft, complete_observation
from test_observation import current, execution, draft


def inputs(remaining=1):
    results = [execution()]
    state = current(results)
    return ReplanningInput(planning_input=state["planning_input"],
                           plan=state["investigation_plan"],
                           observation=complete_observation(results, KnowledgeObservationDraft.model_validate(draft(results))),
                           remaining_rounds=remaining)


def agent_for(payload, *, finish_reason="stop"):
    calls = []

    def create(**options):
        calls.append(options)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=json.dumps(payload)),
        )])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return InvestigationAgent(client, "test-model"), calls


def decide(agent, value, **changes):
    budget = dict(input_limit=16000, context_window=20000, output_tokens=1000, safety_tokens=1000)
    return agent.replan(value, **(budget | changes))


def test_continue_is_only_a_direction_and_does_not_modify_input():
    value = inputs()
    before = value.model_dump_json()
    agent, calls = agent_for(dict(action="CONTINUE", reason="补查不同资料", next_focus="标签成熟周期文档"))
    decision = decide(agent, value)
    assert decision.next_focus == "标签成熟周期文档"
    assert value.model_dump_json() == before
    assert calls[0]["max_tokens"] == 1000
    assert json.loads(calls[0]["messages"][1]["content"]) == value.model_dump(mode="json")
    assert "tools" not in calls[0]


def test_exhausted_round_budget_stops_without_model_call():
    agent, calls = agent_for({})
    decision = decide(agent, inputs(0))
    assert decision.action == "STOP" and not calls
    assert "不代表根因已确认" in decision.reason


@pytest.mark.parametrize("payload", [
    dict(action="CONTINUE", reason="继续"),
    dict(action="CONTINUE", reason="继续", next_focus="  "),
    dict(action="STOP", reason="结束", next_focus="不应再补查"),
    dict(action="STOP", reason="结束", investigation_id="forged"),
    dict(action="CLOSE_CASE", reason="声称根因已确认"),
])
def test_invalid_model_decisions_are_not_accepted(payload):
    agent, _ = agent_for(payload)
    with pytest.raises(ValidationError):
        decide(agent, inputs())


def test_truncated_response_is_not_a_decision():
    agent, _ = agent_for(dict(action="STOP", reason="结束"), finish_reason="length")
    with pytest.raises(ValueError, match="未完整返回"):
        decide(agent, inputs())


def test_wrong_observation_task_is_rejected_before_call():
    value = inputs().model_dump(mode="json")
    value["plan"]["tasks"][0]["task_id"] = "another-task"
    with pytest.raises(ValidationError, match="完整任务序列"):
        ReplanningInput.model_validate(value)


def test_oversized_necessary_material_is_not_clipped_or_sent():
    value = inputs()
    agent, calls = agent_for(dict(action="STOP", reason="结束"))
    with pytest.raises(ValueError, match="token 预算"):
        decide(agent, value, input_limit=1)
    assert not calls


def test_token_estimate_counts_mixed_language_and_budget_reserves_output():
    assert estimate_tokens("中文 abcd 😀") == 4
    assert estimate_tokens("") == 0
    messages = [{"role": "user", "content": "hello"}]
    assert check_request_budget(messages, input_limit=1000, context_window=2000,
                                output_tokens=500, safety_tokens=100) > 0
    with pytest.raises(ValueError):
        check_request_budget(messages, input_limit=1000, context_window=100,
                             output_tokens=100, safety_tokens=1)
