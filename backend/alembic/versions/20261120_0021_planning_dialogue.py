"""Append-only planning dialogue for progressive skill loading."""
from alembic import op
import sqlalchemy as sa

revision = "20261120_0021"
down_revision = "20261118_0020"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("investigation_planning_dialogue",
        sa.Column("run_id", sa.String(128), sa.ForeignKey("investigation_planning_runs.run_id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("sequence >= 0 AND schema_version = 1", name="ck_planning_dialogue_sequence"))
    op.execute("""CREATE FUNCTION planning_dialogue_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'Planning dialogue is immutable'; END IF;
      IF encode(sha256(convert_to(NEW.payload, 'UTF8')), 'hex') <> NEW.input_digest
        THEN RAISE EXCEPTION 'Planning dialogue digest mismatch'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER planning_dialogue_integrity BEFORE INSERT OR UPDATE OR DELETE
    ON investigation_planning_dialogue FOR EACH ROW EXECUTE FUNCTION planning_dialogue_immutable();""")


def downgrade():
    op.drop_table("investigation_planning_dialogue")
    op.execute("DROP FUNCTION planning_dialogue_immutable()")
