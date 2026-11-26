"""请求生成阶段的只读交互；关联正式运行和任务。"""
from alembic import op
import sqlalchemy as sa

revision = "20261125_0026"
down_revision = "20261125_0025"
branch_labels = depends_on = None


def upgrade():
    op.create_table("investigation_query_reads",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["investigation_runs.run_id"]),
        sa.CheckConstraint("sequence >= 0", name="ck_query_reads_sequence"))
    op.execute("CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON investigation_query_reads "
        "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")


def downgrade():
    raise RuntimeError("先明确只读交互记录的数据退役范围")
