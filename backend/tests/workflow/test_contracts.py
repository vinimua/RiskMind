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
        window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 10, 1, tzinfo=timezone.utc),
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


@pytest.mark.parametrize("changes", [
    {"window_end": "2026-09-01T00:00:00Z"},
    {"window_end": "2026-08-01T00:00:00Z"},
    {"window_start": "2026-09-01T00:00:00"},
    {"current_value": float("nan")},
])
def test_alert_rejects_invalid_window_and_values(changes):
    data = _monitoring_alert().model_dump(mode="json")
    with pytest.raises(ValidationError): MonitoringAlert.model_validate({**data, **changes})


def test_alert_normalizes_offsets_for_stable_digest():
    from risk_platform.workflow.persistence.contracts import input_digest
    alert = _monitoring_alert()
    data = alert.model_dump(mode="json")
    shifted = MonitoringAlert.model_validate({**data,
        "window_start": "2026-09-01T08:00:00+08:00",
        "window_end": "2026-10-01T08:00:00+08:00",
        "triggered_at": "2026-10-01T08:00:00+08:00"})
    assert input_digest(shifted) == input_digest(alert)


def test_legacy_alert_roundtrip_keeps_historical_payload_and_digest():
    import json
    from risk_platform.workflow.persistence.contracts import InvestigationRecord, canonical_json, input_digest
    data = _monitoring_alert().model_dump(mode="json")
    data.pop("window_start"); data.pop("window_end")
    data["time_window"] = "2026-09"
    old = {"user_query": None, "monitoring_alert": data,
           "available_capabilities": ["KNOWLEDGE_RETRIEVAL"]}
    source = InitialPlanningInput.model_validate(old)
    assert json.loads(canonical_json(source)) == old
    import hashlib
    historical_json = json.dumps(old, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert input_digest(source) == hashlib.sha256(historical_json.encode("utf-8")).hexdigest()
    record = InvestigationRecord(investigation_id="old", created_by="A", planning_input=source)
    assert InvestigationRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError): MonitoringAlert.model_validate(data)
