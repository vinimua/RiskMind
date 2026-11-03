"""Persist graph-internal initial planning before AcceptedPlan exists."""
from alembic import op

revision = "20261104_0007"
down_revision = "20261103_0006"
branch_labels = None
depends_on = None


def upgrade():
    for sql in DDL:
        op.execute(sql)
    op.execute("""CREATE FUNCTION planning_integrity() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; root investigation_planning_runs%ROWTYPE; fact investigation_planning_attempts%ROWTYPE;
    BEGIN
      IF TG_TABLE_NAME='investigation_planning_runs' THEN
        body := NEW.payload::jsonb;
        IF NEW.schema_version<>1 OR body->>'schema_version' IS DISTINCT FROM '1'
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
    END $$""")
    for table in ("investigation_planning_runs", "investigation_planning_acceptances"):
        op.execute(f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
        op.execute(f"CREATE TRIGGER planning_integrity BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION planning_integrity()")
    op.execute("CREATE TRIGGER immutable_history BEFORE DELETE ON investigation_planning_attempts FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
    op.execute("CREATE TRIGGER planning_integrity BEFORE INSERT OR UPDATE ON investigation_planning_attempts FOR EACH ROW EXECUTE FUNCTION planning_integrity()")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM investigation_planning_runs)
      THEN RAISE EXCEPTION 'cannot discard planning history'; END IF; END $$""")
    for table in ("investigation_planning_acceptances", "investigation_planning_attempts", "investigation_planning_runs"):
        op.drop_table(table)
    op.execute("DROP FUNCTION planning_integrity()")


DDL = [
    """CREATE TABLE investigation_planning_runs (
	run_id VARCHAR(128) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_id VARCHAR(128) NOT NULL,
	plan_id VARCHAR(128) NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (run_id),
	FOREIGN KEY(investigation_id, round_id) REFERENCES investigation_rounds (investigation_id, round_id),
	CONSTRAINT uq_planning_identity UNIQUE (run_id, plan_id, investigation_id, round_id),
	CONSTRAINT ck_planning_schema CHECK (schema_version = 1),
	UNIQUE (round_id),
	UNIQUE (plan_id)
)""",
    """CREATE TABLE investigation_planning_attempts (
	attempt_id VARCHAR(128) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	attempt_number INTEGER NOT NULL,
	actor_id VARCHAR(128) NOT NULL,
	claim_id VARCHAR(128) NOT NULL,
	attribution TEXT NOT NULL,
	attribution_digest VARCHAR(64) NOT NULL,
	status VARCHAR(16) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	resolution_code VARCHAR(128),
	candidate TEXT,
	candidate_digest VARCHAR(64),
	PRIMARY KEY (attempt_id),
	FOREIGN KEY(run_id) REFERENCES investigation_planning_runs (run_id),
	CONSTRAINT uq_planning_number UNIQUE (run_id, attempt_number),
	CONSTRAINT uq_planning_attempt_run UNIQUE (attempt_id, run_id),
	CONSTRAINT ck_planning_number CHECK (attempt_number > 0),
	CONSTRAINT ck_planning_status CHECK ((status = 'STARTED' AND ended_at IS NULL AND resolution_code IS NULL AND candidate IS NULL AND candidate_digest IS NULL) OR (status = 'COMMITTED' AND ended_at >= started_at AND ended_at IS NOT NULL AND resolution_code IS NULL AND candidate IS NOT NULL AND candidate_digest IS NOT NULL) OR (status IN ('FAILED','UNCERTAIN') AND ended_at >= started_at AND ended_at IS NOT NULL AND resolution_code IS NOT NULL AND candidate IS NULL AND candidate_digest IS NULL))
)""",
    """CREATE UNIQUE INDEX uq_planning_committed ON investigation_planning_attempts (run_id) WHERE status = 'COMMITTED'""",
    """CREATE UNIQUE INDEX uq_planning_started ON investigation_planning_attempts (run_id) WHERE status = 'STARTED'""",
    """CREATE TABLE investigation_planning_acceptances (
	run_id VARCHAR(128) NOT NULL,
	attempt_id VARCHAR(128) NOT NULL,
	plan_id VARCHAR(128) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_id VARCHAR(128) NOT NULL,
	actor_id VARCHAR(128) NOT NULL,
	PRIMARY KEY (run_id),
	FOREIGN KEY(run_id, plan_id, investigation_id, round_id) REFERENCES investigation_planning_runs (run_id, plan_id, investigation_id, round_id),
	FOREIGN KEY(attempt_id, run_id) REFERENCES investigation_planning_attempts (attempt_id, run_id),
	FOREIGN KEY(plan_id, investigation_id, round_id) REFERENCES investigation_plans (plan_id, investigation_id, round_id),
	UNIQUE (attempt_id),
	UNIQUE (plan_id)
)""",
]
