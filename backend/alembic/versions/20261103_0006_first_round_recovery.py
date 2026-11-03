"""First-round claims, pinned result choices and v2 phase artifacts."""
from alembic import op

revision = "20261103_0006"
down_revision = "20261103_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_ledger_result", "investigation_results", type_="check")
    op.create_check_constraint("ck_ledger_result", "investigation_results",
        "schema_version IN (1,2) AND NOT (execution_id IS NOT NULL AND observation_id IS NOT NULL)")
    op.create_unique_constraint("uq_ledger_result_operation", "investigation_results", ["result_id", "operation_key"])
    for statement in DDL:
        op.execute(statement)
    op.execute("""CREATE FUNCTION first_round_generation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.current_generation IS NOT NULL THEN RAISE EXCEPTION 'new run has no claim'; END IF;
        ELSIF (to_jsonb(NEW)-'current_generation') IS DISTINCT FROM (to_jsonb(OLD)-'current_generation')
           OR NEW.current_generation IS DISTINCT FROM COALESCE(OLD.current_generation,0)+1
        THEN RAISE EXCEPTION 'run identity immutable; generation must advance once'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER run_generation BEFORE INSERT OR UPDATE ON investigation_runs "
               "FOR EACH ROW EXECUTE FUNCTION first_round_generation()")
    op.execute("CREATE TRIGGER immutable_history BEFORE DELETE ON investigation_runs "
               "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
    for table in ("investigation_run_claims", "investigation_run_attempts", "investigation_run_selections"):
        op.execute(f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
    for table in ("investigation_runs", "investigation_run_claims"):
        op.execute(f"CREATE TRIGGER payload_integrity BEFORE INSERT ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION ledger_payload_check()")
    op.execute("""CREATE FUNCTION first_round_binding() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE op_row investigation_operations%ROWTYPE; run_row investigation_runs%ROWTYPE; actor text;
    BEGIN
        SELECT * INTO STRICT run_row FROM investigation_runs WHERE run_id=NEW.run_id;
        IF TG_TABLE_NAME='investigation_run_attempts' THEN
            SELECT o.* INTO STRICT op_row FROM investigation_operations o JOIN investigation_attempts a
                ON a.operation_key=o.operation_key WHERE a.attempt_id=NEW.attempt_id;
            SELECT actor_id INTO actor FROM investigation_attempts WHERE attempt_id=NEW.attempt_id;
            IF actor IS DISTINCT FROM (SELECT actor_id FROM investigation_run_claims
                WHERE run_id=NEW.run_id AND generation=NEW.generation)
            THEN RAISE EXCEPTION 'attempt actor differs from claim'; END IF;
        ELSE
            SELECT * INTO STRICT op_row FROM investigation_operations WHERE operation_key=NEW.operation_key;
        END IF;
        IF (op_row.investigation_id,op_row.round_id,op_row.plan_id)
           IS DISTINCT FROM (run_row.investigation_id,run_row.round_id,run_row.plan_id)
           OR NEW.generation IS DISTINCT FROM run_row.current_generation
        THEN RAISE EXCEPTION 'run binding belongs to another plan or stale claim'; END IF;
        RETURN NEW;
    END $$""")
    for table in ("investigation_run_attempts", "investigation_run_selections"):
        op.execute(f"CREATE TRIGGER run_binding BEFORE INSERT ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION first_round_binding()")
    body = PREVIOUS_PAYLOAD_CHECK.replace("'task_id','operation_type']", "'task_id','operation_type','run_id','generation','actor_id']")
    old = """ELSIF body->'query_request' IS NULL OR body->'query_request' = 'null'::jsonb
                  OR NEW.execution_id IS NOT NULL OR NEW.observation_id IS NOT NULL THEN
                RAISE EXCEPTION 'query operation requires query artifact';"""
    new = """ELSIF body->'query_request' IS NULL OR body->'query_request' = 'null'::jsonb THEN
                IF NEW.schema_version <> 2 OR NEW.observation_id IS NOT NULL OR NEW.execution_id IS NULL
                   OR (body#>>'{execution,execution_id}') IS DISTINCT FROM NEW.execution_id
                   OR (body#>>'{execution,task_id}') IS DISTINCT FROM op_row_placeholder.task_id
                   OR (body#>>'{execution,capability}') IS DISTINCT FROM 'KNOWLEDGE_RETRIEVAL'
                   OR (body#>>'{execution,status}') IS DISTINCT FROM 'FAILED'
                   OR strpos(COALESCE(body#>>'{execution,error,code}',''),'QUERY_FORMULATION_') <> 1
                THEN RAISE EXCEPTION 'query failure artifact invalid'; END IF;
            ELSIF NEW.execution_id IS NOT NULL OR NEW.observation_id IS NOT NULL THEN
                RAISE EXCEPTION 'query request cannot impersonate execution';""".replace("op_row_placeholder", "operation")
    assert old in body
    op.execute(body.replace(old, new))


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM investigation_runs) OR EXISTS(SELECT 1 FROM investigation_results WHERE schema_version=2)
        THEN RAISE EXCEPTION 'cannot discard first-round recovery history'; END IF;
    END $$""")
    op.execute(PREVIOUS_PAYLOAD_CHECK)
    op.execute("DROP FUNCTION first_round_binding() CASCADE")
    op.execute("DROP FUNCTION first_round_generation() CASCADE")
    op.drop_constraint("fk_run_current_claim", "investigation_runs", type_="foreignkey")
    for table in ("investigation_run_selections", "investigation_run_attempts", "investigation_run_claims", "investigation_runs"):
        op.drop_table(table)
    op.drop_constraint("uq_ledger_result_operation", "investigation_results", type_="unique")
    op.drop_constraint("ck_ledger_result", "investigation_results", type_="check")
    op.create_check_constraint("ck_ledger_result", "investigation_results",
        "schema_version = 1 AND NOT (execution_id IS NOT NULL AND observation_id IS NOT NULL)")

PREVIOUS_PAYLOAD_CHECK = """CREATE OR REPLACE FUNCTION ledger_payload_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; field text; operation investigation_operations%ROWTYPE; count_artifacts int;
    BEGIN
        body := NEW.payload::jsonb;
        IF NEW.schema_version NOT IN (1,2) OR (body->>'schema_version') IS DISTINCT FROM NEW.schema_version::text
           OR NEW.input_digest <> encode(sha256(convert_to(NEW.payload, 'UTF8')), 'hex')
        THEN RAISE EXCEPTION 'invalid ledger payload digest or schema'; END IF;
        IF TG_TABLE_NAME <> 'investigation_results' THEN
            FOREACH field IN ARRAY ARRAY['investigation_id','round_id','round_number','plan_id','plan_version','task_id','operation_type'] LOOP
                IF to_jsonb(NEW) ? field AND (to_jsonb(NEW)->field) IS DISTINCT FROM (body->field)
                THEN RAISE EXCEPTION 'ledger identity differs from payload'; END IF;
            END LOOP;
        ELSE
            SELECT * INTO STRICT operation FROM investigation_operations WHERE operation_key = NEW.operation_key;
            count_artifacts := (CASE WHEN body->'query_request' <> 'null'::jsonb THEN 1 ELSE 0 END)
                             + (CASE WHEN body->'execution' <> 'null'::jsonb THEN 1 ELSE 0 END)
                             + (CASE WHEN body->'observation' <> 'null'::jsonb THEN 1 ELSE 0 END);
            IF count_artifacts <> 1 THEN RAISE EXCEPTION 'result must have exactly one artifact'; END IF;
            IF operation.operation_type = 'KNOWLEDGE_EXECUTION' THEN
                IF body->'execution' IS NULL OR body->'execution' = 'null'::jsonb
                   OR (body#>>'{execution,task_id}') IS DISTINCT FROM operation.task_id
                   OR (body#>>'{execution,capability}') IS DISTINCT FROM 'KNOWLEDGE_RETRIEVAL'
                   OR (body#>>'{execution,execution_id}') IS DISTINCT FROM NEW.execution_id
                   OR NEW.execution_id IS NULL OR NEW.observation_id IS NOT NULL
                THEN RAISE EXCEPTION 'execution identity differs from operation'; END IF;
            ELSIF operation.operation_type = 'OBSERVATION' THEN
                IF body->'observation' IS NULL OR body->'observation' = 'null'::jsonb
                   OR (body#>>'{observation,observation_id}') IS DISTINCT FROM NEW.observation_id
                   OR NEW.observation_id IS NULL OR NEW.execution_id IS NOT NULL
                THEN RAISE EXCEPTION 'observation identity differs from operation'; END IF;
            ELSIF body->'query_request' IS NULL OR body->'query_request' = 'null'::jsonb
                  OR NEW.execution_id IS NOT NULL OR NEW.observation_id IS NOT NULL THEN
                RAISE EXCEPTION 'query operation requires query artifact';
            END IF;
        END IF;
        RETURN NEW;
    END $$"""

DDL = (
    """CREATE TABLE investigation_runs (
	run_id VARCHAR(128) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_id VARCHAR(128) NOT NULL,
	plan_id VARCHAR(128) NOT NULL,
	current_generation INTEGER,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (run_id),
	FOREIGN KEY(plan_id, investigation_id, round_id) REFERENCES investigation_plans (plan_id, investigation_id, round_id),
	CONSTRAINT uq_first_round_run UNIQUE (round_id),
	CONSTRAINT ck_run_generation CHECK (schema_version = 1 AND (current_generation IS NULL OR current_generation > 0))
)""",
    """CREATE TABLE investigation_run_claims (
	run_id VARCHAR(128) NOT NULL,
	generation INTEGER NOT NULL,
	actor_id VARCHAR(128) NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (run_id, generation),
	FOREIGN KEY(run_id) REFERENCES investigation_runs (run_id),
	CONSTRAINT ck_run_claim CHECK (schema_version = 1 AND generation > 0)
)""",
    """CREATE TABLE investigation_run_attempts (
	attempt_id VARCHAR(128) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	generation INTEGER NOT NULL,
	PRIMARY KEY (attempt_id),
	FOREIGN KEY(attempt_id) REFERENCES investigation_attempts (attempt_id),
	FOREIGN KEY(run_id, generation) REFERENCES investigation_run_claims (run_id, generation)
)""",
    """CREATE TABLE investigation_run_selections (
	run_id VARCHAR(128) NOT NULL,
	operation_key VARCHAR(64) NOT NULL,
	result_id VARCHAR(128) NOT NULL,
	generation INTEGER NOT NULL,
	PRIMARY KEY (run_id, operation_key),
	FOREIGN KEY(run_id, generation) REFERENCES investigation_run_claims (run_id, generation),
	FOREIGN KEY(result_id, operation_key) REFERENCES investigation_results (result_id, operation_key)
)""",
    """ALTER TABLE investigation_runs ADD CONSTRAINT fk_run_current_claim FOREIGN KEY(run_id, current_generation) REFERENCES investigation_run_claims (run_id, generation) DEFERRABLE INITIALLY DEFERRED""",
)
