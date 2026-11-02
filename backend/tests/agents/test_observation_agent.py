import json
from types import SimpleNamespace

import pytest

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.contracts import InvestigationPlan


def agent(content, finish="stop"):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])
    return InvestigationAgent(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test-model"), calls


def test_observation_prompt_and_structured_response():
    content = {"execution_refs": ["E"], "findings": [], "knowledge_gaps": [],
               "next_step_suggestions": [], "narrative": "尚未验证当前案件。"}
    model, calls = agent(json.dumps(content, ensure_ascii=False))
    result = model.observe_knowledge_round(InvestigationPlan(goal="背景", tasks=[]), [])
    assert result.narrative == content["narrative"]
    system = calls[0]["messages"][0]["content"]
    assert "历史报告只能解释为历史知识线索" in system and "待分析数据" in system
    assert "不得虚构来源" in system
    data = json.loads(calls[0]["messages"][1]["content"])
    assert "output_schema" in data and "actor_id" not in data and "authorize" not in data


@pytest.mark.parametrize("content,finish", [(None, "stop"), ("", "stop"), ("{}", "length"), ("not-json", "stop"),
                                          ('{"root_cause":"confirmed"}', "stop")])
def test_invalid_or_truncated_explanation_not_accepted(content, finish):
    model, _ = agent(content, finish)
    with pytest.raises(ValueError):
        model.observe_knowledge_round(InvestigationPlan(goal="背景", tasks=[]), [])
