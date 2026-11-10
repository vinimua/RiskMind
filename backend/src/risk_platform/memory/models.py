"""派生记忆和阶段上下文快照；迁移触发器禁止原地修改。"""
from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column
from risk_platform.db.database import Base
from risk_platform.workflow.persistence.models import PayloadRow


class CaseMemoryRow(PayloadRow, Base):
    __tablename__ = "case_memories"
    memory_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_result_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    __table_args__ = (
        ForeignKeyConstraint(["investigation_id", "round_id"], ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        ForeignKeyConstraint(["source_result_id"], ["investigation_results.result_id"]),
        CheckConstraint("schema_version IN (1,2,3)", name="ck_case_memory_schema"),
    )


class ContextSnapshotRow(PayloadRow, Base):
    __tablename__ = "agent_context_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    investigation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(128), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["investigation_id"], ["investigation_ledger.investigation_id"]),
        UniqueConstraint("run_id", "stage", name="uq_context_run_stage"),
        CheckConstraint("schema_version IN (1,2,3,4,5,6)", name="ck_context_snapshot_schema"),
    )
