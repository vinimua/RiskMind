"""个人文档替代旧的逐轮聊天记忆；不生成旧记忆的长期副本。"""
from alembic import op
import sqlalchemy as sa

revision = "20261124_0023"
down_revision = "20261121_0022"
branch_labels = None
depends_on = None


def upgrade():
    # 用户已授权退役旧载荷；只改普通回答的旧决策形状，不改回答和入口动作。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE chat_entry_decisions DISABLE TRIGGER protect_entry_decision")
    op.execute("""WITH converted AS (
        SELECT turn_id, '{"answer":' || to_json(decision_payload::jsonb->>'answer')::text ||
            ',"memory_candidate":' || 'null}' AS payload
        FROM chat_entry_decisions WHERE decision_payload::jsonb ? 'answer'
    ) UPDATE chat_entry_decisions d SET decision_payload=c.payload,
        decision_digest=encode(sha256(convert_to(c.payload,'UTF8')),'hex')
        FROM converted c WHERE d.turn_id=c.turn_id""")
    op.execute("ALTER TABLE chat_entry_decisions ENABLE TRIGGER protect_entry_decision")
    op.execute("SET CONSTRAINTS ALL DEFERRED")
    op.drop_constraint("ck_chat_memory", "chat_turns", type_="check")
    op.drop_constraint("ck_chat_status", "chat_turns", type_="check")
    op.drop_column("chat_turns", "memory_payload")
    op.drop_column("chat_turns", "memory_digest")
    op.create_check_constraint("ck_chat_status", "chat_turns",
        "(status='STARTED' AND response_payload IS NULL AND response_digest IS NULL AND ended_at IS NULL) OR "
        "(status IN ('SUCCEEDED','FAILED') AND response_payload IS NOT NULL AND response_digest IS NOT NULL AND ended_at IS NOT NULL)")
    op.create_unique_constraint("uq_chat_turn_actor", "chat_turns", ["turn_id","actor_id"])
    op.create_table("personal_memory_notes",
        sa.Column("note_id",sa.String(128),primary_key=True),
        sa.Column("owner_id",sa.String(128),nullable=False),
        sa.Column("title",sa.String(120),nullable=False),
        sa.Column("description",sa.String(300),nullable=False),
        sa.Column("content",sa.Text(),nullable=False),
        sa.Column("version",sa.Integer(),nullable=False),
        sa.Column("active",sa.Boolean(),nullable=False),
        sa.UniqueConstraint("owner_id","title",name="uq_personal_note_title"),
        sa.CheckConstraint("version>0",name="ck_personal_note_version"))
    op.create_index("ix_personal_memory_notes_owner_id","personal_memory_notes",["owner_id"])
    op.create_table("personal_memory_revisions",
        sa.Column("note_id",sa.String(128),primary_key=True),
        sa.Column("version",sa.Integer(),primary_key=True),
        sa.Column("source_id",sa.String(128),nullable=False,unique=True),
        sa.Column("payload",sa.Text(),nullable=False),
        sa.ForeignKeyConstraint(["note_id"],["personal_memory_notes.note_id"]))
    op.execute("CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON personal_memory_revisions "
               "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
    op.create_table("personal_memory_settings",
        sa.Column("owner_id",sa.String(128),primary_key=True),
        sa.Column("read_enabled",sa.Boolean(),nullable=False),
        sa.Column("auto_write_enabled",sa.Boolean(),nullable=False))
    op.create_table("personal_memory_maintenance",
        sa.Column("turn_id",sa.String(128),primary_key=True),
        sa.Column("owner_id",sa.String(128),nullable=False),
        sa.Column("candidate",sa.Text(),nullable=False),
        sa.Column("status",sa.String(16),nullable=False),
        sa.Column("result",sa.Text()),
        sa.ForeignKeyConstraint(["turn_id","owner_id"],["chat_turns.turn_id","chat_turns.actor_id"]),
        sa.CheckConstraint("status IN ('PENDING','DONE','FAILED','DISABLED')",name="ck_memory_maintenance_status"))


def downgrade():
    raise RuntimeError("旧聊天记忆已明确退役，不能通过降级恢复被删除的载荷")
