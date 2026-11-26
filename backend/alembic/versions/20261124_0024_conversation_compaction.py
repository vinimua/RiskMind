"""为会话压缩增加稳定轮次顺序和不可变摘要；原文不裁剪。"""
from alembic import op
import sqlalchemy as sa

revision = "20261124_0024"
down_revision = "20261124_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_turns", sa.Column("turn_number", sa.Integer(), nullable=True))
    # 回填新位置字段，不修改已有正文、状态或摘要；延迟约束先执行，避免 DDL 遇到待处理事件。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE chat_turns DISABLE TRIGGER protect_chat_turn")
    op.execute("""WITH numbered AS (
        SELECT turn_id, row_number() OVER (PARTITION BY conversation_id ORDER BY created_at, turn_id) AS number
        FROM chat_turns
    ) UPDATE chat_turns t SET turn_number=n.number FROM numbered n WHERE t.turn_id=n.turn_id""")
    op.execute("ALTER TABLE chat_turns ENABLE TRIGGER protect_chat_turn")
    op.execute("SET CONSTRAINTS ALL DEFERRED")
    op.alter_column("chat_turns", "turn_number", nullable=False, server_default="0")
    op.create_unique_constraint("uq_chat_turn_number", "chat_turns", ["conversation_id", "turn_number"])
    op.create_unique_constraint("uq_chat_turn_position", "chat_turns", ["turn_id", "conversation_id", "actor_id", "turn_number"])
    op.create_check_constraint("ck_chat_turn_number", "chat_turns", "turn_number > 0")
    op.execute("""CREATE FUNCTION allocate_chat_turn_number() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP='UPDATE' THEN
        IF NEW.turn_number IS DISTINCT FROM OLD.turn_number THEN
          RAISE EXCEPTION 'chat turn position is immutable';
        END IF;
      ELSE
        PERFORM pg_advisory_xact_lock(hashtextextended('riskmind:chat:' || NEW.conversation_id,0));
        SELECT coalesce(max(turn_number),0)+1 INTO NEW.turn_number FROM chat_turns
          WHERE conversation_id=NEW.conversation_id;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER allocate_chat_turn_number BEFORE INSERT OR UPDATE ON chat_turns
      FOR EACH ROW EXECUTE FUNCTION allocate_chat_turn_number();""")
    op.create_table("chat_conversation_summaries",
        sa.Column("conversation_id",sa.String(128),primary_key=True),
        sa.Column("version",sa.Integer(),primary_key=True),
        sa.Column("owner_id",sa.String(128),nullable=False),
        sa.Column("through_turn_id",sa.String(128),nullable=False),
        sa.Column("through_turn_number",sa.Integer(),nullable=False),
        sa.Column("generated_by_turn_id",sa.String(128),nullable=False),
        sa.Column("content",sa.Text(),nullable=False),
        sa.Column("content_digest",sa.String(64),nullable=False),
        sa.ForeignKeyConstraint(["conversation_id","owner_id"],["chat_conversations.conversation_id","chat_conversations.owner_id"]),
        sa.ForeignKeyConstraint(["through_turn_id","conversation_id","owner_id","through_turn_number"],
            ["chat_turns.turn_id","chat_turns.conversation_id","chat_turns.actor_id","chat_turns.turn_number"]),
        sa.ForeignKeyConstraint(["generated_by_turn_id"],["chat_turns.turn_id"]),
        sa.CheckConstraint("version>0 AND through_turn_number>0",name="ck_conversation_summary_position"))
    op.execute("CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON chat_conversation_summaries "
               "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")


def downgrade():
    raise RuntimeError("已保存的会话压缩边界不能被静默删除，请先明确数据退役方案")
