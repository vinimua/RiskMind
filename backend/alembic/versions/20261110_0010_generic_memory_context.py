"""Typed generic memory context v2; do not rewrite existing immutable snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "20261110_0010"
down_revision = "20261109_0009"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version IN (1,2)")


def downgrade():
    count = op.get_bind().scalar(sa.text("SELECT count(*) FROM agent_context_snapshots WHERE schema_version=2"))
    if count:
        raise RuntimeError("v2 snapshots exist; downgrade would invalidate immutable records")
    op.drop_constraint("ck_context_snapshot_schema", "agent_context_snapshots", type_="check")
    op.create_check_constraint("ck_context_snapshot_schema", "agent_context_snapshots", "schema_version=1")
