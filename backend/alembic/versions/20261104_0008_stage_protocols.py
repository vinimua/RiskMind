"""Separate run identity and stage protocols; preserve all v1 history."""
from alembic import op

revision = "20261104_0008"
down_revision = "20261104_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_run_generation", "investigation_runs", type_="check")
    op.create_check_constraint("ck_run_generation", "investigation_runs",
        "schema_version IN (1,2) AND (current_generation IS NULL OR current_generation > 0)")
    op.drop_constraint("ck_planning_schema", "investigation_planning_runs", type_="check")
    op.create_check_constraint("ck_planning_schema", "investigation_planning_runs", "schema_version IN (1,2)")
    op.execute(NEW_TRIGGER)


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS(SELECT 1 FROM investigation_runs WHERE schema_version=2)
         OR EXISTS(SELECT 1 FROM investigation_planning_runs WHERE schema_version=2)
      THEN RAISE EXCEPTION 'cannot discard v2 run or planning history'; END IF;
    END $$""")
    op.execute(OLD_TRIGGER)
    op.drop_constraint("ck_planning_schema", "investigation_planning_runs", type_="check")
    op.create_check_constraint("ck_planning_schema", "investigation_planning_runs", "schema_version = 1")
    op.drop_constraint("ck_run_generation", "investigation_runs", type_="check")
    op.create_check_constraint("ck_run_generation", "investigation_runs",
        "schema_version = 1 AND (current_generation IS NULL OR current_generation > 0)")


NEW_TRIGGER = """CREATE OR REPLACE FUNCTION planning_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; root investigation_planning_runs%ROWTYPE; fact investigation_planning_attempts%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME='investigation_planning_runs' THEN
        body := NEW.payload::jsonb;
        IF NEW.schema_version NOT IN (1,2) OR body->>'schema_version' IS DISTINCT FROM NEW.schema_version::text
           OR NEW.input_digest IS DISTINCT FROM encode(sha256(convert_to(NEW.payload,'UTF8')),'hex')
           OR (body#>>'{execution,run_id}',body#>>'{execution,plan_id}',body#>>'{execution,investigation_id}',body#>>'{execution,round_id}')
              IS DISTINCT FROM (NEW.run_id,NEW.plan_id,NEW.investigation_id,NEW.round_id)
        THEN RAISE EXCEPTION 'planning input integrity'; END IF;
      ELSIF TG_TABLE_NAME='investigation_planning_attempts' THEN
        IF TG_OP='INSERT' AND NEW.status<>'STARTED' THEN RAISE EXCEPTION 'planning attempt starts STARTED'; END IF;
        IF TG_OP='UPDATE' AND (OLD.status<>'STARTED' OR NEW.status='STARTED'
          OR (to_jsonb(NEW)-ARRAY['status','ended_at','resolution_code','candidate','candidate_digest'])
             IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','ended_at','resolution_code','candidate','candidate_digest']))
        THEN RAISE EXCEPTION 'planning terminal attempt immutable'; END IF;
        SELECT * INTO STRICT root FROM investigation_planning_runs WHERE run_id=NEW.run_id;
        body := NEW.attribution::jsonb;
        IF NEW.attribution_digest IS DISTINCT FROM encode(sha256(convert_to(NEW.attribution,'UTF8')),'hex')
           OR body->>'schema_version' IS DISTINCT FROM '1'
           OR body->>'action' IS DISTINCT FROM 'investigation:continue'
           OR (body->>'investigation_id',body->>'round_id',body->>'actor_id',body#>>'{execution_context,actor_id}')
              IS DISTINCT FROM (root.investigation_id,root.round_id,NEW.actor_id,NEW.actor_id)
        THEN RAISE EXCEPTION 'planning actor attribution'; END IF;
        IF NEW.status='COMMITTED' AND (NEW.candidate_digest IS DISTINCT FROM encode(sha256(convert_to(NEW.candidate,'UTF8')),'hex')
           OR NEW.candidate::jsonb->>'schema_version' IS DISTINCT FROM '1'
           OR jsonb_typeof(NEW.candidate::jsonb#>'{plan,tasks}') IS DISTINCT FROM 'array')
        THEN RAISE EXCEPTION 'planning candidate integrity'; END IF;
      ELSE
        SELECT * INTO STRICT fact FROM investigation_planning_attempts WHERE attempt_id=NEW.attempt_id;
        SELECT payload::jsonb INTO STRICT body FROM investigation_plans WHERE plan_id=NEW.plan_id;
        IF fact.status<>'COMMITTED' OR fact.candidate::jsonb->'plan' IS DISTINCT FROM body->'plan'
           OR length(btrim(NEW.actor_id))=0 THEN RAISE EXCEPTION 'accepted plan differs from candidate'; END IF;
      END IF;
      RETURN NEW;
    END $$"""

OLD_TRIGGER = NEW_TRIGGER.replace(
    "NEW.schema_version NOT IN (1,2) OR body->>'schema_version' IS DISTINCT FROM NEW.schema_version::text",
    "NEW.schema_version<>1 OR body->>'schema_version' IS DISTINCT FROM '1'",
)
