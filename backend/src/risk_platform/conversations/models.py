"""会话及一问一答记录；用户消息先提交，回答和派生记忆随后原子提交。"""
from datetime import datetime
from sqlalchemy import String, Text, DateTime, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Index, text
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
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_payload: Mapped[str | None] = mapped_column(Text)
    response_digest: Mapped[str | None] = mapped_column(String(64))
    memory_payload: Mapped[str | None] = mapped_column(Text)
    memory_digest: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "actor_id"], ["chat_conversations.conversation_id", "chat_conversations.owner_id"]),
        CheckConstraint("action IN ('CHAT','READ_INVESTIGATION','START_INVESTIGATION')", name="ck_chat_action"),
        CheckConstraint("(status='STARTED' AND response_payload IS NULL AND response_digest IS NULL AND ended_at IS NULL AND memory_payload IS NULL AND memory_digest IS NULL) OR "
                        "(status IN ('SUCCEEDED','FAILED') AND response_payload IS NOT NULL AND response_digest IS NOT NULL AND ended_at IS NOT NULL)", name="ck_chat_status"),
        CheckConstraint("(memory_payload IS NULL AND memory_digest IS NULL) OR (status='SUCCEEDED' AND memory_payload IS NOT NULL AND memory_digest IS NOT NULL)", name="ck_chat_memory"),
        Index("uq_chat_active", "conversation_id", unique=True, postgresql_where=text("status='STARTED'")),
    )
