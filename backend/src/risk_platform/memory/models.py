"""派生记忆和阶段上下文快照；迁移触发器禁止原地修改。"""
from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint, CheckConstraint, Text, Integer, Boolean
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


class PersonalNoteRow(Base):
    """用户当前有效的记忆文档；修订正文另存，不覆盖历史版本。"""
    __tablename__ = "personal_memory_notes"
    note_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    __table_args__ = (CheckConstraint("version > 0", name="ck_personal_note_version"),
                     UniqueConstraint("owner_id", "title", name="uq_personal_note_title"))


class PersonalNoteRevisionRow(Base):
    __tablename__ = "personal_memory_revisions"
    note_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (ForeignKeyConstraint(["note_id"], ["personal_memory_notes.note_id"]),)


class PersonalMemorySettingsRow(Base):
    __tablename__ = "personal_memory_settings"
    owner_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    read_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    auto_write_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)


class MemoryMaintenanceRow(Base):
    """候选与回答同事务保存；处理失败可重试，不重新调用回答模型。"""
    __tablename__ = "personal_memory_maintenance"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    result: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (ForeignKeyConstraint(["turn_id", "owner_id"], ["chat_turns.turn_id", "chat_turns.actor_id"]),
        CheckConstraint("status IN ('PENDING','DONE','FAILED','DISABLED')", name="ck_memory_maintenance_status"))
