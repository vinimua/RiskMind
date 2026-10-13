import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

# Workflow module initialization creates the real SDK client but these tests
# replace every model call with a fake and never access the network.
os.environ.setdefault("ZHIPU_API_KEY", "test-only-key")

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow import nodes
from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    MonitoringAlert,
    TaskStatus,
    UserQuery,
)
from risk_platform.workflow.graph import build_investigation_graph


class FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


class FakeClient:
    def __init__(self, content: str):
        self.completions = FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


def _plan_json(capability: CapabilityType = CapabilityType.KNOWLEDGE_RETRIEVAL) -> str:
    return json.dumps(
        {
            "goal": "建立当前异常的初始调查背景",
            "tasks": [
                {
                    "objective": "检索历史类似异常案例及对应调查过程",
                    "capability": capability.value,
                    "expected_result": "获得历史相似异常和已有调查路径",
                }
            ],
        },
        ensure_ascii=False,
    )


def _investigation_plan() -> InvestigationPlan:
    return InvestigationPlan(
        goal="调查 credit_v3 KS 下降原因",
        tasks=[
            InvestigationTask(
                objective="检索历史 KS 下降异常案例及对应调查过程",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="获得历史相似异常、常见影响因素以及已有调查路径",
            ),
            InvestigationTask(
                objective="检索 credit_v3 的模型背景、已知限制和历史问题",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="获得与当前 KS 异常相关的模型知识和历史经验",
            ),
        ],
    )


def _available_capabilities() -> list[CapabilityType]:
    return [
        CapabilityType.KNOWLEDGE_RETRIEVAL,
        CapabilityType.FACT_RETRIEVAL,
        CapabilityType.COMPUTATION,
    ]


def _user_planning_input() -> InitialPlanningInput:
    return InitialPlanningInput(
        user_query=UserQuery(
            query="为什么 credit_v3 最近 KS 下降？",
            model_id="credit_v3",
        ),
        available_capabilities=_available_capabilities(),
    )


def _monitoring_alert() -> MonitoringAlert:
    return MonitoringAlert(
        alert_id="ALERT-001",
        model_id="credit_v3",
        metric="KS",
        symptom="DEGRADATION",
        time_window="2026-09",
        current_value=0.31,
        reference_value=0.42,
        triggered_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_investigation_agent_plans_user_query_knowledge_tasks() -> None:
    client = FakeClient(_plan_json())
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")

    plan = agent.initial_plan(_user_planning_input())

    assert len(client.completions.calls) == 1
    call = client.completions.calls[0]
    assert call["model"] == "glm-4.7-flash"
    assert call["response_format"] == {"type": "json_object"}
    system_prompt = call["messages"][0]["content"]
    assert "不生成 Hypothesis" in system_prompt
    assert "只能创建 capability 为 KNOWLEDGE_RETRIEVAL" in system_prompt
    assert "不得创建 FACT_RETRIEVAL 或 COMPUTATION" in system_prompt
    assert "source = USER_QUERY" in call["messages"][1]["content"]
    assert "query = 为什么 credit_v3 最近 KS 下降？" in call["messages"][1]["content"]
    assert "model_id = credit_v3" in call["messages"][1]["content"]
    assert "time_window = not provided" in call["messages"][1]["content"]
    assert set(InvestigationPlan.model_fields) == {"goal", "tasks"}
    assert all(
        task.capability is CapabilityType.KNOWLEDGE_RETRIEVAL
        for task in plan.tasks
    )
    assert str(UUID(plan.tasks[0].task_id)) == plan.tasks[0].task_id
    assert plan.tasks[0].status is TaskStatus.PENDING


def test_investigation_agent_plans_monitoring_alert_knowledge_tasks() -> None:
    client = FakeClient(_plan_json())
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")
    alert = _monitoring_alert()
    planning_input = InitialPlanningInput(
        monitoring_alert=alert,
        available_capabilities=_available_capabilities(),
    )

    plan = agent.initial_plan(planning_input)

    context = client.completions.calls[0]["messages"][1]["content"]
    assert "source = MONITORING_ALERT" in context
    assert "source = USER_QUERY" not in context
    assert "alert_id = ALERT-001" in context
    assert "model_id = credit_v3" in context
    assert "metric = KS" in context
    assert "symptom = DEGRADATION" in context
    assert "time_window = 2026-09" in context
    assert "current_value = 0.31" in context
    assert "reference_value = 0.42" in context
    assert "triggered_at = 2026-10-01T00:00:00+00:00" in context
    assert all(
        task.capability is CapabilityType.KNOWLEDGE_RETRIEVAL
        for task in plan.tasks
    )


def test_investigation_agent_requires_knowledge_retrieval_capability() -> None:
    client = FakeClient(_plan_json())
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")
    planning_input = InitialPlanningInput(
        user_query=UserQuery(query="为什么 KS 下降？"),
        available_capabilities=[
            CapabilityType.FACT_RETRIEVAL,
            CapabilityType.COMPUTATION,
        ],
    )

    with pytest.raises(ValueError, match="需要 KNOWLEDGE_RETRIEVAL 能力"):
        agent.initial_plan(planning_input)

    assert client.completions.calls == []


def test_investigation_agent_rejects_forbidden_task_capability() -> None:
    client = FakeClient(_plan_json(CapabilityType.COMPUTATION))
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")

    with pytest.raises(ValueError, match="禁止的任务能力: COMPUTATION"):
        agent.initial_plan(_user_planning_input())


def test_investigation_agent_propagates_invalid_json_error() -> None:
    client = FakeClient("not-json")
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")

    with pytest.raises(ValidationError):
        agent.initial_plan(_user_planning_input())


def test_investigation_agent_rejects_empty_tasks() -> None:
    content = json.dumps(
        {"goal": "建立当前异常的初始调查背景", "tasks": []},
        ensure_ascii=False,
    )
    client = FakeClient(content)
    agent = InvestigationAgent(client=client, model="glm-4.7-flash")

    with pytest.raises(ValueError, match="至少一个任务"):
        agent.initial_plan(_user_planning_input())


def test_initial_planning_input_accepts_user_query() -> None:
    planning_input = _user_planning_input()

    assert planning_input.user_query is not None
    assert planning_input.user_query.model_id == "credit_v3"
    assert planning_input.monitoring_alert is None


def test_initial_planning_input_accepts_monitoring_alert() -> None:
    alert = _monitoring_alert()

    planning_input = InitialPlanningInput(
        monitoring_alert=alert,
        available_capabilities=_available_capabilities(),
    )

    assert planning_input.monitoring_alert == alert
    assert planning_input.user_query is None


def test_initial_planning_input_rejects_missing_source() -> None:
    with pytest.raises(ValidationError):
        InitialPlanningInput(available_capabilities=_available_capabilities())


def test_initial_planning_input_rejects_multiple_sources() -> None:
    with pytest.raises(ValidationError):
        InitialPlanningInput(
            user_query=UserQuery(query="为什么 KS 下降？"),
            monitoring_alert=_monitoring_alert(),
            available_capabilities=_available_capabilities(),
        )


def test_investigation_task_defaults_are_system_managed() -> None:
    task = _investigation_plan().tasks[0]

    assert str(UUID(task.task_id)) == task.task_id
    assert task.status is TaskStatus.PENDING


def test_investigation_plan_contains_only_goal_and_tasks() -> None:
    plan = _investigation_plan()

    assert set(InvestigationPlan.model_fields) == {"goal", "tasks"}
    assert all(
        task.capability is CapabilityType.KNOWLEDGE_RETRIEVAL
        for task in plan.tasks
    )


def test_capability_types_round_trip_through_json() -> None:
    for capability in CapabilityType:
        task = InvestigationTask(
            objective="测试调查任务",
            capability=capability,
            expected_result="得到测试结果",
        )

        restored = InvestigationTask.model_validate_json(task.model_dump_json())

        assert restored.capability is capability


def test_initial_planning_delegates_to_investigation_agent(monkeypatch) -> None:
    received_inputs: list[InitialPlanningInput] = []
    planning_input = _user_planning_input()
    plan = _investigation_plan()

    def fake_initial_plan(value: InitialPlanningInput) -> InvestigationPlan:
        received_inputs.append(value)
        return plan

    monkeypatch.setattr(nodes.investigation_agent, "initial_plan", fake_initial_plan)

    result = nodes.initial_planning({"planning_input": planning_input})

    assert received_inputs == [planning_input]
    assert result["investigation_plan"] == plan


def test_investigation_graph_compiles() -> None:
    graph = build_investigation_graph()

    assert graph is not None


def test_investigation_graph_runs_to_v0_completion(monkeypatch) -> None:
    planning_input = _user_planning_input()
    plan = _investigation_plan()
    monkeypatch.setattr(
        nodes.investigation_agent,
        "initial_plan",
        lambda value: plan,
    )
    graph = build_investigation_graph()

    result = graph.invoke({"planning_input": planning_input})

    assert result == {
        "planning_input": planning_input,
        "investigation_plan": plan,
        "investigation_complete": True,
    }
