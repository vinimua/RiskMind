"""Controlled tool approvals; immutable materials, decisions and append-only audit."""
from alembic import op
import sqlalchemy as sa

revision = "20261118_0020"
down_revision = "20261115_0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("controlled_actions",
        sa.Column("action_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("investigation_runs.run_id"), nullable=False),
        sa.Column("plan_id", sa.String(128), sa.ForeignKey("investigation_plans.plan_id"), nullable=False),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("approved", sa.Boolean()), sa.Column("decided_by", sa.String(128)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("result_id", sa.String(128), sa.ForeignKey("investigation_results.result_id")),
        sa.Column("resolution", sa.Text()),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("run_id", "task_id", name="uq_controlled_action_task"),
        sa.CheckConstraint("schema_version = 1 AND status IN ('PENDING_CONFIRMATION','APPROVED','REJECTED','EXPIRED','INVALIDATED','EXECUTING','UNCERTAIN','SUCCEEDED','FAILED')", name="ck_controlled_action_state"))
    op.create_table("controlled_action_audit",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(128), sa.ForeignKey("controlled_actions.action_id"), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False))
    op.execute("""CREATE FUNCTION controlled_action_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; plan investigation_plans; run investigation_runs;
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'Actions cannot be deleted'; END IF;
      IF TG_OP = 'UPDATE' AND (NEW.payload,NEW.input_digest,NEW.action_id,NEW.run_id,NEW.plan_id,NEW.task_id,NEW.created_at,NEW.schema_version)
          IS DISTINCT FROM (OLD.payload,OLD.input_digest,OLD.action_id,OLD.run_id,OLD.plan_id,OLD.task_id,OLD.created_at,OLD.schema_version)
      THEN RAISE EXCEPTION 'Approval material is immutable'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.approved IS NOT NULL AND
          (NEW.approved,NEW.decided_by,NEW.decided_at) IS DISTINCT FROM (OLD.approved,OLD.decided_by,OLD.decided_at)
      THEN RAISE EXCEPTION 'Approval decision is immutable'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.result_id IS NOT NULL AND
          (NEW.result_id,NEW.status) IS DISTINCT FROM (OLD.result_id,OLD.status)
      THEN RAISE EXCEPTION 'Completed action is immutable'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.resolution IS NOT NULL AND NEW.resolution IS DISTINCT FROM OLD.resolution
      THEN RAISE EXCEPTION 'Manual resolution is immutable'; END IF;
      body := NEW.payload::jsonb;
      SELECT * INTO plan FROM investigation_plans WHERE plan_id=NEW.plan_id;
      SELECT * INTO run FROM investigation_runs WHERE run_id=NEW.run_id;
      IF NEW.input_digest IS DISTINCT FROM encode(sha256(convert_to(NEW.payload,'UTF8')),'hex')
         OR body->>'action_id' IS DISTINCT FROM NEW.action_id
         OR body->>'run_id' IS DISTINCT FROM NEW.run_id
         OR body->>'plan_id' IS DISTINCT FROM NEW.plan_id
         OR body->>'task_id' IS DISTINCT FROM NEW.task_id
         OR plan.investigation_id IS DISTINCT FROM body->>'investigation_id'
         OR plan.round_id IS DISTINCT FROM body->>'round_id'
         OR run.plan_id IS DISTINCT FROM NEW.plan_id
         OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(plan.payload::jsonb->'plan'->'tasks') t WHERE t->>'task_id'=NEW.task_id)
      THEN RAISE EXCEPTION 'Invalid controlled action association'; END IF;
      RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER controlled_action_integrity BEFORE INSERT OR UPDATE OR DELETE ON controlled_actions FOR EACH ROW EXECUTE FUNCTION controlled_action_integrity()")
    op.execute("""CREATE FUNCTION controlled_audit_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Action audit is append only'; END $$""")
    op.execute("CREATE TRIGGER controlled_audit_immutable BEFORE UPDATE OR DELETE ON controlled_action_audit FOR EACH ROW EXECUTE FUNCTION controlled_audit_immutable()")


def downgrade():
    op.drop_table("controlled_action_audit")
    op.drop_table("controlled_actions")
    op.execute("DROP FUNCTION controlled_audit_immutable()")
    op.execute("DROP FUNCTION controlled_action_integrity()")
