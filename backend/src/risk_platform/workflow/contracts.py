"""Shared contracts for the investigation workflow."""

from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class CapabilityType(StrEnum):
    KNOWLEDGE_RETRIEVAL = "KNOWLEDGE_RETRIEVAL"
    FACT_RETRIEVAL = "FACT_RETRIEVAL"
    COMPUTATION = "COMPUTATION"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


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
