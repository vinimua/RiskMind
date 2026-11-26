"""Shared contracts for the investigation workflow."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Self
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


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


class LegacyMonitoringAlert(BaseModel):
    """仅用于读取已有调查输入；保留旧字符串窗口，不猜测其时区和起止时间。"""
    model_config = ConfigDict(extra="forbid")
    alert_id: str
    model_id: str
    metric: str
    symptom: str
    time_window: str
    current_value: float
    reference_value: float
    triggered_at: datetime


class MonitoringAlert(BaseModel):
    """已判定的不可变告警事件；统计窗口为带时区的 [start, end)。"""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    alert_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    symptom: str = Field(min_length=1, max_length=4000)
    window_start: AwareDatetime
    window_end: AwareDatetime
    current_value: float
    # 上游含义尚未确定：保留原名，不自动解释为阈值或基准分布。
    reference_value: float
    triggered_at: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("统计窗口开始时间必须早于结束时间")
        # 同一时刻的不同偏移写法归一化，避免重投告警产生不同摘要。
        self.window_start = self.window_start.astimezone(timezone.utc)
        self.window_end = self.window_end.astimezone(timezone.utc)
        self.triggered_at = self.triggered_at.astimezone(timezone.utc)
        return self


class InitialPlanningInput(BaseModel):
    user_query: UserQuery | None = None
    monitoring_alert: MonitoringAlert | LegacyMonitoringAlert | None = None
    available_capabilities: list[CapabilityType]

    @model_validator(mode="after")
    def validate_investigation_source(self) -> Self:
        if (self.user_query is None) == (self.monitoring_alert is None):
            raise ValueError(
                "exactly one of user_query or monitoring_alert must be provided"
            )
        return self


class SkillMaterialRef(BaseModel):
    """任务引用本轮已读取的方法文件；正文归规划账本所有。"""
    model_config = ConfigDict(extra="forbid", frozen=True)
    skill_name: str = Field(min_length=1)
    resource: str = Field(min_length=1)


class InvestigationTask(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    objective: str
    capability: CapabilityType
    expected_result: str
    skill_refs: tuple[SkillMaterialRef, ...] = ()
    status: TaskStatus = TaskStatus.PENDING


class InvestigationPlan(BaseModel):
    goal: str
    tasks: list[InvestigationTask]
