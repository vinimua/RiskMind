from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

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


def _investigation_plan() -> InvestigationPlan:
    return InvestigationPlan(
        goal="调查 credit_v3 KS 下降原因",
        hypotheses=["标签成熟度不足", "客群结构变化", "特征漂移"],
        tasks=[
            InvestigationTask(
                objective="确认当前评估样本标签是否成熟",
                capability=CapabilityType.COMPUTATION,
                expected_result="得到当前标签成熟状态",
            ),
            InvestigationTask(
                objective="确认近期是否发生模型或特征版本变化",
                capability=CapabilityType.FACT_RETRIEVAL,
                expected_result="得到当前版本和近期变更记录",
            ),
            InvestigationTask(
                objective="搜索历史类似 KS 下降案例",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="得到相关历史 Case 和已有调查经验",
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
