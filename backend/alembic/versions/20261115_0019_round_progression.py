"""Immutable observation-to-successor links for the investigation graph."""
from alembic import op
import sqlalchemy as sa

revision = "20261115_0019"
down_revision = "20261114_0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("investigation_round_progressions",
        sa.Column("source_round_id", sa.String(128), primary_key=True),
        sa.Column("investigation_id", sa.String(128), nullable=False),
        sa.Column("root_run_id", sa.String(128), nullable=False),
        sa.Column("source_run_id", sa.String(128), nullable=False, unique=True),
        sa.Column("observation_result_id", sa.String(128), nullable=False, unique=True),
        sa.Column("next_round_id", sa.String(128), unique=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("input_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["investigation_id", "source_round_id"], ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        sa.ForeignKeyConstraint(["investigation_id", "next_round_id"], ["investigation_rounds.investigation_id", "investigation_rounds.round_id"]),
        sa.ForeignKeyConstraint(["root_run_id"], ["investigation_runs.run_id"]),
        sa.ForeignKeyConstraint(["source_run_id"], ["investigation_runs.run_id"]),
        sa.ForeignKeyConstraint(["observation_result_id"], ["investigation_results.result_id"]),
        sa.CheckConstraint("schema_version = 1", name="ck_progression_schema"))
    op.execute("""CREATE FUNCTION progression_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Round progression is immutable'; END $$""")
    op.execute("""CREATE TRIGGER progression_immutable BEFORE UPDATE OR DELETE ON investigation_round_progressions
        FOR EACH ROW EXECUTE FUNCTION progression_immutable()""")
    op.execute("""CREATE FUNCTION progression_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE body jsonb; source_operation investigation_operations; source_run investigation_runs;
                root_run investigation_runs; source_round investigation_rounds; target_round investigation_rounds;
        BEGIN
            body := NEW.payload::jsonb;
            SELECT * INTO source_run FROM investigation_runs WHERE run_id=NEW.source_run_id;
            SELECT * INTO root_run FROM investigation_runs WHERE run_id=NEW.root_run_id;
            SELECT * INTO source_round FROM investigation_rounds WHERE round_id=NEW.source_round_id;
            SELECT o.* INTO source_operation FROM investigation_operations o
                JOIN investigation_results r ON r.operation_key=o.operation_key
                JOIN investigation_run_selections s ON s.result_id=r.result_id AND s.run_id=NEW.source_run_id
                WHERE r.result_id=NEW.observation_result_id AND r.observation_id IS NOT NULL;
            IF NEW.input_digest IS DISTINCT FROM encode(sha256(convert_to(NEW.payload,'UTF8')),'hex')
               OR body->>'schema_version' IS DISTINCT FROM '1'
               OR body->>'source_round_id' IS DISTINCT FROM NEW.source_round_id
               OR body->>'source_run_id' IS DISTINCT FROM NEW.source_run_id
               OR body->>'root_run_id' IS DISTINCT FROM NEW.root_run_id
               OR body->>'investigation_id' IS DISTINCT FROM NEW.investigation_id
               OR body->>'observation_result_id' IS DISTINCT FROM NEW.observation_result_id
               OR body->>'next_round_id' IS DISTINCT FROM NEW.next_round_id
               OR source_run.round_id IS DISTINCT FROM NEW.source_round_id
               OR root_run.investigation_id IS DISTINCT FROM NEW.investigation_id
               OR source_operation.round_id IS DISTINCT FROM NEW.source_round_id
               OR source_operation.plan_id IS DISTINCT FROM source_run.plan_id
               OR source_operation.operation_type IS DISTINCT FROM 'OBSERVATION'
            THEN RAISE EXCEPTION 'Invalid progression source'; END IF;
            IF NEW.next_round_id IS NOT NULL THEN
                SELECT * INTO target_round FROM investigation_rounds WHERE round_id=NEW.next_round_id;
                IF target_round.investigation_id IS DISTINCT FROM NEW.investigation_id
                   OR target_round.round_number IS DISTINCT FROM source_round.round_number+1
                   OR body->>'next_run_id' IS NULL OR body->>'next_plan_id' IS NULL
                   OR body->>'stop_reason' IS NOT NULL
                THEN RAISE EXCEPTION 'Invalid progression target'; END IF;
            ELSIF NULLIF(btrim(body->>'stop_reason'),'') IS NULL
                  OR body->>'next_run_id' IS NOT NULL OR body->>'next_plan_id' IS NOT NULL
            THEN RAISE EXCEPTION 'Invalid progression stop'; END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER progression_integrity BEFORE INSERT ON investigation_round_progressions
        FOR EACH ROW EXECUTE FUNCTION progression_integrity()""")


def downgrade():
    raise RuntimeError("Retire immutable round progression records explicitly before downgrade")
