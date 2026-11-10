"""Remove unused coverage counters from current memory snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "20261110_0014"
down_revision = "20261110_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version IN (1,2,3,4,5,6)")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM agent_context_snapshots WHERE schema_version=6)")):
        raise RuntimeError("v6 snapshots exist; downgrade requires an explicit data policy")
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version IN (1,2,3,4,5)")
