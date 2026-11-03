"""Immutable investigation execution ledger; no workflow/checkpointer integration."""
from alembic import op

revision = "20261103_0004"
down_revision = "20261025_0003"
branch_labels = None
depends_on = None


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.execute("""CREATE FUNCTION ledger_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'ledger history is immutable';
    END $$""")
    op.execute("""CREATE FUNCTION ledger_attempt_transition() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'STARTED' THEN RAISE EXCEPTION 'attempt must start as STARTED'; END IF;
            RETURN NEW;
        END IF;
        IF OLD.status <> 'STARTED' OR NEW.status = 'STARTED'
           OR (to_jsonb(NEW) - ARRAY['status','ended_at','result_id','resolution_code'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status','ended_at','result_id','resolution_code'])
        THEN RAISE EXCEPTION 'invalid attempt transition'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION ledger_payload_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; field text; operation investigation_operations%ROWTYPE; count_artifacts int;
    BEGIN
        body := NEW.payload::jsonb;
        IF NEW.schema_version <> 1 OR (body->>'schema_version') IS DISTINCT FROM '1'
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
    END $$""")
    op.execute("""CREATE FUNCTION ledger_task_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE expected jsonb; parent_payload text;
    BEGIN
        SELECT payload INTO parent_payload FROM investigation_plans WHERE plan_id = NEW.plan_id;
        IF parent_payload IS NULL THEN RETURN NEW; END IF; -- FK reports a missing plan.
        expected := parent_payload::jsonb#>'{plan,tasks}'->NEW.ordinal;
        IF (expected->>'task_id') IS DISTINCT FROM NEW.task_id
           OR (expected->>'capability') IS DISTINCT FROM NEW.capability
        THEN RAISE EXCEPTION 'task differs from accepted plan'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("""CREATE FUNCTION ledger_plan_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (SELECT count(*) FROM investigation_plan_tasks WHERE plan_id = NEW.plan_id)
           <> jsonb_array_length(NEW.payload::jsonb#>'{plan,tasks}')
        THEN RAISE EXCEPTION 'accepted plan tasks are incomplete'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER task_membership BEFORE INSERT ON investigation_plan_tasks "
               "FOR EACH ROW EXECUTE FUNCTION ledger_task_check()")
    op.execute("CREATE CONSTRAINT TRIGGER plan_completeness AFTER INSERT ON investigation_plans "
               "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION ledger_plan_complete()")
    for table in TABLES:
        if table != "investigation_attempts":
            op.execute(f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
        else:
            op.execute(f"CREATE TRIGGER immutable_history BEFORE DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION ledger_immutable()")
            op.execute(f"CREATE TRIGGER attempt_transition BEFORE INSERT OR UPDATE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION ledger_attempt_transition()")
        if table not in {"investigation_attempts", "investigation_plan_tasks"}:
            op.execute(f"CREATE TRIGGER payload_integrity BEFORE INSERT ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION ledger_payload_check()")


TABLES = ("investigation_ledger", "investigation_rounds", "investigation_plans", "investigation_plan_tasks",
          "investigation_operations", "investigation_attempts", "investigation_results")


def downgrade():
    # Empty disposable ledgers can be removed; populated audit history cannot be silently discarded.
    condition = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in TABLES)
    op.execute(f"DO $$ BEGIN IF {condition} THEN RAISE EXCEPTION 'cannot discard execution ledger history'; END IF; END $$")
    op.execute("DROP FUNCTION ledger_payload_check() CASCADE")
    op.execute("DROP FUNCTION ledger_attempt_transition() CASCADE")
    op.execute("DROP FUNCTION ledger_immutable() CASCADE")
    op.execute("DROP FUNCTION ledger_task_check() CASCADE")
    op.execute("DROP FUNCTION ledger_plan_complete() CASCADE")
    op.drop_constraint("fk_ledger_attempt_result", "investigation_attempts", type_="foreignkey")
    op.drop_constraint("fk_ledger_result_committed_attempt", "investigation_results", type_="foreignkey")
    for table in reversed(TABLES):
        op.drop_table(table)

# Frozen PostgreSQL DDL: this migration never imports evolving runtime models.
DDL = (
    """CREATE TABLE investigation_ledger (
	investigation_id VARCHAR(128) NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (investigation_id),
	CONSTRAINT ck_ledger_investigation_schema CHECK (schema_version = 1)
)""",
    """CREATE TABLE investigation_rounds (
	round_id VARCHAR(128) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_number INTEGER NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (round_id),
	FOREIGN KEY(investigation_id) REFERENCES investigation_ledger (investigation_id),
	CONSTRAINT uq_ledger_round_identity UNIQUE (investigation_id, round_id),
	CONSTRAINT uq_ledger_round_number UNIQUE (investigation_id, round_number),
	CONSTRAINT ck_ledger_round CHECK (schema_version = 1 AND round_number > 0)
)""",
    """CREATE TABLE investigation_plans (
	plan_id VARCHAR(128) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_id VARCHAR(128) NOT NULL,
	plan_version INTEGER NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (plan_id),
	FOREIGN KEY(investigation_id, round_id) REFERENCES investigation_rounds (investigation_id, round_id),
	CONSTRAINT uq_ledger_plan_identity UNIQUE (plan_id, investigation_id, round_id),
	CONSTRAINT uq_ledger_plan_version UNIQUE (round_id, plan_version),
	CONSTRAINT ck_ledger_plan CHECK (schema_version = 1 AND plan_version > 0)
)""",
    """CREATE TABLE investigation_plan_tasks (
	plan_id VARCHAR(128) NOT NULL,
	task_id VARCHAR(128) NOT NULL,
	ordinal INTEGER NOT NULL,
	capability VARCHAR(64) NOT NULL,
	PRIMARY KEY (plan_id, task_id),
	FOREIGN KEY(plan_id) REFERENCES investigation_plans (plan_id),
	CONSTRAINT uq_ledger_task_ordinal UNIQUE (plan_id, ordinal),
	CONSTRAINT ck_ledger_task_ordinal CHECK (ordinal >= 0)
)""",
    """CREATE TABLE investigation_operations (
	operation_key VARCHAR(64) NOT NULL,
	investigation_id VARCHAR(128) NOT NULL,
	round_id VARCHAR(128) NOT NULL,
	plan_id VARCHAR(128) NOT NULL,
	task_id VARCHAR(128),
	operation_type VARCHAR(32) NOT NULL,
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (operation_key),
	FOREIGN KEY(plan_id, investigation_id, round_id) REFERENCES investigation_plans (plan_id, investigation_id, round_id),
	FOREIGN KEY(plan_id, task_id) REFERENCES investigation_plan_tasks (plan_id, task_id),
	CONSTRAINT ck_ledger_operation_schema CHECK (schema_version = 1),
	CONSTRAINT ck_ledger_operation_type CHECK ((operation_type = 'OBSERVATION' AND task_id IS NULL) OR (operation_type IN ('QUERY_FORMULATION', 'KNOWLEDGE_EXECUTION') AND task_id IS NOT NULL))
)""",
    """CREATE UNIQUE INDEX uq_ledger_round_observation ON investigation_operations (plan_id) WHERE operation_type = 'OBSERVATION'""",
    """CREATE UNIQUE INDEX uq_ledger_task_operation ON investigation_operations (plan_id, task_id, operation_type) WHERE task_id IS NOT NULL""",
    """CREATE TABLE investigation_attempts (
	attempt_id VARCHAR(128) NOT NULL,
	operation_key VARCHAR(64) NOT NULL,
	attempt_number INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	result_id VARCHAR(128),
	resolution_code VARCHAR(128),
	started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (attempt_id),
	FOREIGN KEY(operation_key) REFERENCES investigation_operations (operation_key),
	CONSTRAINT uq_ledger_attempt_number UNIQUE (operation_key, attempt_number),
	CONSTRAINT uq_ledger_attempt_operation UNIQUE (attempt_id, operation_key),
	CONSTRAINT uq_ledger_attempt_result UNIQUE (attempt_id, result_id),
	CONSTRAINT ck_ledger_attempt_status CHECK (attempt_number > 0 AND status IN ('STARTED','COMMITTED','FAILED','UNCERTAIN')),
	CONSTRAINT ck_ledger_attempt_resolution CHECK ((status = 'STARTED' AND ended_at IS NULL AND result_id IS NULL AND resolution_code IS NULL) OR (status = 'COMMITTED' AND ended_at IS NOT NULL AND ended_at >= started_at AND result_id IS NOT NULL AND resolution_code IS NULL) OR (status IN ('FAILED','UNCERTAIN') AND ended_at IS NOT NULL AND ended_at >= started_at AND result_id IS NULL AND resolution_code IS NOT NULL))
)""",
    """CREATE UNIQUE INDEX uq_ledger_started_attempt ON investigation_attempts (operation_key) WHERE status = 'STARTED'""",
    """CREATE TABLE investigation_results (
	result_id VARCHAR(128) NOT NULL,
	attempt_id VARCHAR(128) NOT NULL,
	operation_key VARCHAR(64) NOT NULL,
	execution_id VARCHAR(128),
	observation_id VARCHAR(128),
	schema_version INTEGER NOT NULL,
	payload TEXT NOT NULL,
	input_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (result_id),
	FOREIGN KEY(attempt_id, operation_key) REFERENCES investigation_attempts (attempt_id, operation_key),
	CONSTRAINT uq_ledger_result_attempt UNIQUE (result_id, attempt_id),
	CONSTRAINT ck_ledger_result CHECK (schema_version = 1 AND NOT (execution_id IS NOT NULL AND observation_id IS NOT NULL)),
	UNIQUE (attempt_id),
	UNIQUE (execution_id),
	UNIQUE (observation_id)
)""",
    """ALTER TABLE investigation_attempts ADD CONSTRAINT fk_ledger_attempt_result FOREIGN KEY(result_id, attempt_id) REFERENCES investigation_results (result_id, attempt_id) DEFERRABLE INITIALLY DEFERRED""",
    """ALTER TABLE investigation_results ADD CONSTRAINT fk_ledger_result_committed_attempt FOREIGN KEY(attempt_id, result_id) REFERENCES investigation_attempts (attempt_id, result_id) DEFERRABLE INITIALLY DEFERRED""",
)
