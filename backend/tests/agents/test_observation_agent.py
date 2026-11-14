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
    content = {"findings": [], "unresolved_questions": [],
               "action": "STOP", "stop_reason": "No further knowledge question"}
    model, calls = agent(json.dumps(content, ensure_ascii=False))
    result = model.observe_round(InvestigationPlan(goal="背景", tasks=[]), [])
    assert result.stop_reason == content["stop_reason"]
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
        model.observe_round(InvestigationPlan(goal="背景", tasks=[]), [])


@pytest.mark.parametrize("changes", [
    {"action": "CONTINUE", "unresolved_questions": []},
    {"action": "CONTINUE", "unresolved_questions": ["Q"], "stop_reason": "stop"},
    {"action": "STOP", "stop_reason": None},
    {"unresolved_questions": ["Q", " Q "]},
    {"next_focus": "obsolete"},
    {"narrative": "obsolete"},
])
def test_observation_decision_fields_have_no_redundant_branches(changes):
    from risk_platform.workflow.observation import ObservationDraft
    value = {"findings": [], "unresolved_questions": [], "action": "STOP", "stop_reason": "done"}
    with pytest.raises(ValueError):
        ObservationDraft.model_validate(value | changes)


def test_next_planning_keeps_multiple_observation_sources_and_failures():
    from datetime import datetime, timezone
    from risk_platform.workflow.observation import RoundObservation, ExecutionObservation
    from risk_platform.workflow.contracts import InitialPlanningInput, UserQuery

    def observed(identity, question, action):
        return RoundObservation(observation_id=identity, created_at=datetime.now(timezone.utc),
            status="SUCCEEDED", execution_summaries=(ExecutionObservation(
                execution_id="E" + identity, task_id="T" + identity, status="SUCCEEDED", outcome="NO_HITS"),),
            findings=(), unresolved_questions=(question,), action=action,
            stop_reason="This agent cannot continue" if action == "STOP" else None)

    observations = [observed("A", "Q1", "STOP"), observed("B", "Q2", "CONTINUE")]
    content = {"goal": "Answer Q2", "tasks": [
        {"objective": "Search version docs", "expected_result": "Version information"},
        {"objective": "Search label docs", "expected_result": "Label information"}]}
    model, calls = agent(json.dumps(content))
    source = InitialPlanningInput(user_query=UserQuery(query="Original goal"),
                                  available_capabilities=["KNOWLEDGE_RETRIEVAL"])
    history = [{"task_id": "T0", "status": "FAILED", "outcome": "EXECUTION_FAILED", "request": None}]
    plan = model.initial_plan(source, previous_observations=observations,
                              previous_tasks=history, remaining_task_budget=2)
    payload = json.loads(calls[0]["messages"][1]["content"])
    assert [o["observation_id"] for o in payload["observations"]] == ["A", "B"]
    assert payload["previous_tasks"] == history and len(plan.tasks) == 2
    with pytest.raises(ValueError, match="预算"):
        model.initial_plan(source, previous_observations=observations,
                           previous_tasks=history, remaining_task_budget=1)
