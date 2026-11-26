"""保存聊天只读交互，不修改旧回答或摘要。"""
from alembic import op
import sqlalchemy as sa

revision = "20261125_0025"
down_revision = "20261124_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("chat_read_interactions",
        sa.Column("turn_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["turn_id"], ["chat_turns.turn_id"]),
        sa.CheckConstraint("sequence >= 0", name="ck_chat_read_sequence"))
    op.execute("CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON chat_read_interactions "
               "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")


def downgrade():
    raise RuntimeError("只读交互包含历史原文，请先明确数据退役范围")
