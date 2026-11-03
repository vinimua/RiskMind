"""运行控制与不可变选择；不另建任务调度器。"""

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from risk_platform.db.database import Base
from .models import PayloadRow


class InvestigationRunRow(PayloadRow, Base):
    __tablename__ = "investigation_runs"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_generation: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(["plan_id", "investigation_id", "round_id"],
            ["investigation_plans.plan_id", "investigation_plans.investigation_id", "investigation_plans.round_id"]),
        ForeignKeyConstraint(["run_id", "current_generation"], ["investigation_run_claims.run_id", "investigation_run_claims.generation"],
            name="fk_run_current_claim", use_alter=True, deferrable=True, initially="DEFERRED"),
        UniqueConstraint("round_id", name="uq_first_round_run"),
        CheckConstraint("schema_version = 1 AND (current_generation IS NULL OR current_generation > 0)", name="ck_run_generation"),
    )


class InvestigationRunClaimRow(PayloadRow, Base):
    __tablename__ = "investigation_run_claims"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["run_id"], ["investigation_runs.run_id"]),
        CheckConstraint("schema_version = 1 AND generation > 0", name="ck_run_claim"),
    )


class InvestigationRunAttemptRow(Base):
    __tablename__ = "investigation_run_attempts"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["attempt_id"], ["investigation_attempts.attempt_id"]),
        ForeignKeyConstraint(["run_id", "generation"], ["investigation_run_claims.run_id", "investigation_run_claims.generation"]),
    )


class InvestigationRunSelectionRow(Base):
    __tablename__ = "investigation_run_selections"
    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["run_id", "generation"], ["investigation_run_claims.run_id", "investigation_run_claims.generation"]),
        ForeignKeyConstraint(["result_id", "operation_key"], ["investigation_results.result_id", "investigation_results.operation_key"]),
    )
