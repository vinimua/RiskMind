"""计划生成前的日志：稳定输入、不可变尝试结果、正式接受来源。"""

from datetime import datetime
from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from risk_platform.db.database import Base
from .models import PayloadRow


class PlanningRunRow(PayloadRow, Base):
    __tablename__ = "investigation_planning_runs"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    __table_args__ = (
        ForeignKeyConstraint(["investigation_id", "round_id"],
            ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        UniqueConstraint("run_id", "plan_id", "investigation_id", "round_id", name="uq_planning_identity"),
        CheckConstraint("schema_version = 1", name="ck_planning_schema"),
    )


class PlanningAttemptRow(Base):
    __tablename__ = "investigation_planning_attempts"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attribution: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_code: Mapped[str | None] = mapped_column(String(128))
    candidate: Mapped[str | None] = mapped_column(Text)
    candidate_digest: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["investigation_planning_runs.run_id"]),
        UniqueConstraint("run_id", "attempt_number", name="uq_planning_number"),
        UniqueConstraint("attempt_id", "run_id", name="uq_planning_attempt_run"),
        CheckConstraint("attempt_number > 0", name="ck_planning_number"),
        CheckConstraint("(status = 'STARTED' AND ended_at IS NULL AND resolution_code IS NULL AND candidate IS NULL AND candidate_digest IS NULL) OR "
            "(status = 'COMMITTED' AND ended_at >= started_at AND ended_at IS NOT NULL AND resolution_code IS NULL AND candidate IS NOT NULL AND candidate_digest IS NOT NULL) OR "
            "(status IN ('FAILED','UNCERTAIN') AND ended_at >= started_at AND ended_at IS NOT NULL AND resolution_code IS NOT NULL AND candidate IS NULL AND candidate_digest IS NULL)", name="ck_planning_status"),
        Index("uq_planning_started", "run_id", unique=True, postgresql_where=text("status = 'STARTED'")),
        Index("uq_planning_committed", "run_id", unique=True, postgresql_where=text("status = 'COMMITTED'")),
    )


class PlanningAcceptanceRow(Base):
    __tablename__ = "investigation_planning_acceptances"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "plan_id", "investigation_id", "round_id"],
            ["investigation_planning_runs.run_id", "investigation_planning_runs.plan_id", "investigation_planning_runs.investigation_id", "investigation_planning_runs.round_id"]),
        ForeignKeyConstraint(["attempt_id", "run_id"], ["investigation_planning_attempts.attempt_id", "investigation_planning_attempts.run_id"]),
        ForeignKeyConstraint(["plan_id", "investigation_id", "round_id"],
            ["investigation_plans.plan_id", "investigation_plans.investigation_id", "investigation_plans.round_id"]),
    )
