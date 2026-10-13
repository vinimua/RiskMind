"""Shared contracts for the investigation workflow."""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class CapabilityType(StrEnum):
    KNOWLEDGE_RETRIEVAL = "KNOWLEDGE_RETRIEVAL"
    FACT_RETRIEVAL = "FACT_RETRIEVAL"
    COMPUTATION = "COMPUTATION"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class UserQuery(BaseModel):
    query: str
    model_id: str | None = None
    time_window: str | None = None


class MonitoringAlert(BaseModel):
    alert_id: str
    model_id: str
    metric: str
    symptom: str
    time_window: str
    current_value: float
    reference_value: float
    triggered_at: datetime


class InitialPlanningInput(BaseModel):
    user_query: UserQuery | None = None
    monitoring_alert: MonitoringAlert | None = None
    available_capabilities: list[CapabilityType]

    @model_validator(mode="after")
    def validate_investigation_source(self) -> Self:
        if (self.user_query is None) == (self.monitoring_alert is None):
            raise ValueError(
                "exactly one of user_query or monitoring_alert must be provided"
            )
        return self


class InvestigationTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    objective: str
    capability: CapabilityType
    expected_result: str
    status: TaskStatus = TaskStatus.PENDING


class InvestigationPlan(BaseModel):
    goal: str
    hypotheses: list[str]
    tasks: list[InvestigationTask]
