from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    MonitoringAlert,
    TaskStatus,
    UserQuery,
)


def _available_capabilities() -> list[CapabilityType]:
    return list(CapabilityType)


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
    planning_input = InitialPlanningInput(
        user_query=UserQuery(query="为什么 credit_v3 最近 KS 下降？", model_id="credit_v3"),
        available_capabilities=_available_capabilities(),
    )

    assert planning_input.user_query is not None
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
    task = InvestigationTask(
        objective="检索历史案例",
        capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
        expected_result="获得历史调查经验",
    )

    assert str(UUID(task.task_id)) == task.task_id
    assert task.status is TaskStatus.PENDING


def test_investigation_plan_contains_only_goal_and_tasks() -> None:
    assert set(InvestigationPlan.model_fields) == {"goal", "tasks"}


def test_capability_types_round_trip_through_json() -> None:
    for capability in CapabilityType:
        task = InvestigationTask(
            objective="测试调查任务",
            capability=capability,
            expected_result="得到测试结果",
        )

        restored = InvestigationTask.model_validate_json(task.model_dump_json())

        assert restored.capability is capability
