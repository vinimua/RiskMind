"""账本表；复合 FK 防止跨轮次、跨计划挂接，终态不可变由迁移触发器保证。"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from risk_platform.db.database import Base


class PayloadRow:
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))


class InvestigationRow(PayloadRow, Base):
    __tablename__ = "investigation_ledger"
    investigation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    __table_args__ = (CheckConstraint("schema_version = 1", name="ck_ledger_investigation_schema"),)


class RoundRow(PayloadRow, Base):
    __tablename__ = "investigation_rounds"
    round_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["investigation_id"], ["investigation_ledger.investigation_id"]),
        UniqueConstraint("investigation_id", "round_id", name="uq_ledger_round_identity"),
        UniqueConstraint("investigation_id", "round_number", name="uq_ledger_round_number"),
        CheckConstraint("schema_version = 1 AND round_number > 0", name="ck_ledger_round"),
    )


class AcceptedPlanRow(PayloadRow, Base):
    __tablename__ = "investigation_plans"
    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["investigation_id", "round_id"], ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        UniqueConstraint("plan_id", "investigation_id", "round_id", name="uq_ledger_plan_identity"),
        UniqueConstraint("round_id", "plan_version", name="uq_ledger_plan_version"),
        CheckConstraint("schema_version = 1 AND plan_version > 0", name="ck_ledger_plan"),
    )


class PlanTaskRow(Base):
    __tablename__ = "investigation_plan_tasks"
    plan_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["plan_id"], ["investigation_plans.plan_id"]),
        UniqueConstraint("plan_id", "ordinal", name="uq_ledger_task_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_ledger_task_ordinal"),
    )


class OperationRow(PayloadRow, Base):
    __tablename__ = "investigation_operations"
    operation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(128))
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128))  # 旧 v1 缺失时不伪造回填。
    __table_args__ = (
        ForeignKeyConstraint(["plan_id", "investigation_id", "round_id"],
                             ["investigation_plans.plan_id", "investigation_plans.investigation_id", "investigation_plans.round_id"]),
        ForeignKeyConstraint(["plan_id", "task_id"], ["investigation_plan_tasks.plan_id", "investigation_plan_tasks.task_id"]),
        CheckConstraint("schema_version IN (1,2) AND (schema_version = 1 OR created_by IS NOT NULL)", name="ck_ledger_operation_schema"),
        CheckConstraint("(operation_type = 'OBSERVATION' AND task_id IS NULL) OR "
                        "(operation_type IN ('QUERY_FORMULATION', 'KNOWLEDGE_EXECUTION') AND task_id IS NOT NULL)",
                        name="ck_ledger_operation_type"),
        Index("uq_ledger_task_operation", "plan_id", "task_id", "operation_type", unique=True,
              postgresql_where=text("task_id IS NOT NULL")),
        Index("uq_ledger_round_observation", "plan_id", unique=True,
              postgresql_where=text("operation_type = 'OBSERVATION'")),
    )


class AttemptRow(Base):
    __tablename__ = "investigation_attempts"
    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result_id: Mapped[str | None] = mapped_column(String(128))
    # 只保存受控代码，不保存 provider body 或原始异常正文。
    resolution_code: Mapped[str | None] = mapped_column(String(128))
    actor_id: Mapped[str | None] = mapped_column(String(128))
    runtime_payload: Mapped[str | None] = mapped_column(Text)
    runtime_digest: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["operation_key"], ["investigation_operations.operation_key"]),
        UniqueConstraint("operation_key", "attempt_number", name="uq_ledger_attempt_number"),
        UniqueConstraint("attempt_id", "operation_key", name="uq_ledger_attempt_operation"),
        UniqueConstraint("attempt_id", "result_id", name="uq_ledger_attempt_result"),
        ForeignKeyConstraint(["result_id", "attempt_id"], ["investigation_results.result_id", "investigation_results.attempt_id"],
                             name="fk_ledger_attempt_result", use_alter=True, deferrable=True, initially="DEFERRED"),
        CheckConstraint("attempt_number > 0 AND status IN ('STARTED','COMMITTED','FAILED','UNCERTAIN')", name="ck_ledger_attempt_status"),
        CheckConstraint("(actor_id IS NULL AND runtime_payload IS NULL AND runtime_digest IS NULL) OR "
                        "(actor_id IS NOT NULL AND runtime_payload IS NOT NULL AND runtime_digest IS NOT NULL)", name="ck_ledger_attempt_actor"),
        CheckConstraint("(status = 'STARTED' AND ended_at IS NULL AND result_id IS NULL AND resolution_code IS NULL) OR "
                        "(status = 'COMMITTED' AND ended_at IS NOT NULL AND ended_at >= started_at AND result_id IS NOT NULL AND resolution_code IS NULL) OR "
                        "(status IN ('FAILED','UNCERTAIN') AND ended_at IS NOT NULL AND ended_at >= started_at AND result_id IS NULL AND resolution_code IS NOT NULL)",
                        name="ck_ledger_attempt_resolution"),
        Index("uq_ledger_started_attempt", "operation_key", unique=True, postgresql_where=text("status = 'STARTED'")),
    )


class ResultRow(PayloadRow, Base):
    __tablename__ = "investigation_results"
    result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    operation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    observation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    __table_args__ = (
        ForeignKeyConstraint(["attempt_id", "operation_key"], ["investigation_attempts.attempt_id", "investigation_attempts.operation_key"]),
        ForeignKeyConstraint(["attempt_id", "result_id"], ["investigation_attempts.attempt_id", "investigation_attempts.result_id"],
                             name="fk_ledger_result_committed_attempt", use_alter=True, deferrable=True, initially="DEFERRED"),
        UniqueConstraint("result_id", "attempt_id", name="uq_ledger_result_attempt"),
        UniqueConstraint("result_id", "operation_key", name="uq_ledger_result_operation"),
        CheckConstraint("schema_version IN (1,2) AND NOT (execution_id IS NOT NULL AND observation_id IS NOT NULL)", name="ck_ledger_result"),
    )
