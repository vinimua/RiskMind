"""Merge pending memory lists; preserve earlier immutable payloads."""
from alembic import op
import sqlalchemy as sa

revision = "20261110_0012"
down_revision = "20261110_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_case_memory_schema", "case_memories", type_="check")
    op.create_check_constraint("ck_case_memory_schema", "case_memories", "schema_version IN (1,2,3)")
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version IN (1,2,3,4)")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM case_memories WHERE schema_version=3) OR "
                                    "EXISTS(SELECT 1 FROM agent_context_snapshots WHERE schema_version=4)")):
        raise RuntimeError("new memory payloads exist; downgrade requires an explicit data policy")
    op.drop_constraint("ck_case_memory_schema", "case_memories", type_="check")
    op.create_check_constraint("ck_case_memory_schema", "case_memories", "schema_version IN (1,2)")
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version IN (1,2,3)")
