"""会话及一问一答记录；用户消息先提交，回答和派生记忆随后原子提交。"""
from datetime import datetime
from sqlalchemy import String, Text, DateTime, Boolean, Integer, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from risk_platform.db.database import Base


class ConversationRow(Base):
    __tablename__ = "chat_conversations"
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    __table_args__ = (UniqueConstraint("conversation_id", "owner_id", name="uq_chat_owner"),)


class ChatTurnRow(Base):
    __tablename__ = "chat_turns"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    turn_number: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_payload: Mapped[str | None] = mapped_column(Text)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    intent_payload: Mapped[str | None] = mapped_column(Text)
    intent_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("(intent_payload IS NULL AND intent_digest IS NULL) OR (action='AUTO' AND intent_payload IS NOT NULL AND intent_digest IS NOT NULL)", name="ck_chat_intent"),
        ForeignKeyConstraint(["conversation_id", "actor_id"], ["chat_conversations.conversation_id", "chat_conversations.owner_id"]),
        CheckConstraint("action IN ('AUTO','CHAT','READ_INVESTIGATION','START_INVESTIGATION','ENTRY_DECISION')", name="ck_chat_action"),
        CheckConstraint("(status='STARTED' AND response_payload IS NULL AND response_digest IS NULL AND ended_at IS NULL) OR "
                        "(status IN ('SUCCEEDED','FAILED') AND response_payload IS NOT NULL AND response_digest IS NOT NULL AND ended_at IS NOT NULL)", name="ck_chat_status"),
        UniqueConstraint("turn_id", "actor_id", name="uq_chat_turn_actor"),
        UniqueConstraint("conversation_id", "turn_number", name="uq_chat_turn_number"),
        UniqueConstraint("turn_id", "conversation_id", "actor_id", "turn_number", name="uq_chat_turn_position"),
        CheckConstraint("turn_number > 0", name="ck_chat_turn_number"),
        Index("uq_chat_active", "conversation_id", unique=True, postgresql_where=text("status='STARTED'")),
    )


class ChatReadRow(Base):
    """本轮只读交互的原始请求与结果；顺序追加，不充当业务执行账本。"""
    __tablename__ = "chat_read_interactions"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (ForeignKeyConstraint(["turn_id"], ["chat_turns.turn_id"]),
                      CheckConstraint("sequence >= 0", name="ck_chat_read_sequence"))


class ConversationSummaryRow(Base):
    """追加式会话摘要；覆盖边界关联原轮次，不删除或复制原始消息。"""
    __tablename__ = "chat_conversation_summaries"
    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    through_turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    through_turn_number: Mapped[int] = mapped_column(Integer, nullable=False)
    generated_by_turn_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "owner_id"], ["chat_conversations.conversation_id", "chat_conversations.owner_id"]),
        ForeignKeyConstraint(["through_turn_id", "conversation_id", "owner_id", "through_turn_number"],
            ["chat_turns.turn_id", "chat_turns.conversation_id", "chat_turns.actor_id", "chat_turns.turn_number"]),
        ForeignKeyConstraint(["generated_by_turn_id"], ["chat_turns.turn_id"]),
        CheckConstraint("version > 0 AND through_turn_number > 0", name="ck_conversation_summary_position"),
    )


class EntryDecisionRow(Base):
    """一次入口决策及最多一次工具调用；不建立第二套调查 Attempt/Result。"""
    __tablename__ = "chat_entry_decisions"
    turn_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_payload: Mapped[str] = mapped_column(Text, nullable=False)
    decision_payload: Mapped[str] = mapped_column(Text, nullable=False)
    decision_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    invocation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    investigation_id: Mapped[str | None] = mapped_column(String(128))
    confirmed: Mapped[bool | None] = mapped_column(Boolean)
    confirmation_actor: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["turn_id"], ["chat_turns.turn_id"]),
        CheckConstraint("status IN ('ANSWERED','REJECTED','PENDING_CONFIRMATION','READY','STARTED','SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')", name="ck_entry_status"),
        CheckConstraint("(confirmed IS NULL AND confirmation_actor IS NULL AND confirmed_at IS NULL) OR (confirmed IS NOT NULL AND confirmation_actor IS NOT NULL AND confirmed_at IS NOT NULL)", name="ck_entry_confirmation"),
        CheckConstraint("(status='ANSWERED' AND invocation_id IS NULL) OR (status<>'ANSWERED' AND invocation_id IS NOT NULL)", name="ck_entry_invocation"),
        CheckConstraint("(status IN ('STARTED','SUCCEEDED','FAILED','UNCERTAIN') AND started_at IS NOT NULL) OR (status NOT IN ('STARTED','SUCCEEDED','FAILED','UNCERTAIN') AND started_at IS NULL)", name="ck_entry_started"),
        CheckConstraint("status <> 'CANCELLED' OR confirmed IS FALSE", name="ck_entry_cancelled"),
    )
