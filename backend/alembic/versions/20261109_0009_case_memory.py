"""Immutable case memory and per-stage context snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "20261109_0009"
down_revision = "20261104_0008"
branch_labels = None
depends_on = None


def upgrade():
    def payload_columns():
        return (sa.Column("schema_version", sa.Integer(), nullable=False),
                sa.Column("payload", sa.Text(), nullable=False),
                sa.Column("input_digest", sa.String(64), nullable=False),
                sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")))
    op.create_table("case_memories",
        sa.Column("memory_id", sa.String(128), primary_key=True),
        sa.Column("investigation_id", sa.String(128), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("source_result_id", sa.String(128), nullable=False, unique=True),
        *payload_columns(),
        sa.ForeignKeyConstraint(["investigation_id", "round_id"], ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        sa.ForeignKeyConstraint(["source_result_id"], ["investigation_results.result_id"]),
        sa.CheckConstraint("schema_version = 1", name="ck_case_memory_schema"))
    op.create_table("agent_context_snapshots",
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("investigation_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("stage", sa.String(128), nullable=False),
        *payload_columns(),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigation_ledger.investigation_id"]),
        sa.UniqueConstraint("run_id", "stage", name="uq_context_run_stage"),
        sa.CheckConstraint("schema_version = 1", name="ck_context_snapshot_schema"))
    op.execute("""CREATE FUNCTION reject_memory_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'memory records are immutable'; END $$""")
    for table in ("case_memories", "agent_context_snapshots"):
        op.execute(f"CREATE TRIGGER immutable_memory BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION reject_memory_mutation()")


def downgrade():
    op.drop_table("agent_context_snapshots")
    op.drop_table("case_memories")
    op.execute("DROP FUNCTION reject_memory_mutation()")
