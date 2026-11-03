"""Per-run identity separation and immutable attempt actor audit; preserve v1 history."""
from alembic import op
import sqlalchemy as sa

revision = "20261103_0005"
down_revision = "20261103_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("investigation_operations", sa.Column("created_by", sa.String(128), nullable=True))
    op.drop_constraint("ck_ledger_operation_schema", "investigation_operations", type_="check")
    op.create_check_constraint("ck_ledger_operation_schema", "investigation_operations",
                               "schema_version IN (1,2) AND (schema_version = 1 OR created_by IS NOT NULL)")
    for column in (sa.Column("actor_id", sa.String(128)), sa.Column("runtime_payload", sa.Text()),
                   sa.Column("runtime_digest", sa.String(64))):
        op.add_column("investigation_attempts", column)
    op.create_check_constraint("ck_ledger_attempt_actor", "investigation_attempts",
        "(actor_id IS NULL AND runtime_payload IS NULL AND runtime_digest IS NULL) OR "
        "(actor_id IS NOT NULL AND runtime_payload IS NOT NULL AND runtime_digest IS NOT NULL)")
    # Only new rows require attribution; migrated history remains honestly unknown.
    op.execute("""CREATE FUNCTION ledger_attempt_actor_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE body jsonb; owner investigation_operations%ROWTYPE;
    BEGIN
        SELECT * INTO STRICT owner FROM investigation_operations WHERE operation_key=NEW.operation_key;
        IF NEW.actor_id IS NULL OR NEW.runtime_payload IS NULL OR NEW.runtime_digest IS NULL
        THEN RAISE EXCEPTION 'new attempt requires authenticated actor attribution'; END IF;
        body := NEW.runtime_payload::jsonb;
        IF NEW.runtime_digest <> encode(sha256(convert_to(NEW.runtime_payload,'UTF8')),'hex')
           OR (body->>'schema_version') IS DISTINCT FROM '1'
           OR (body->>'actor_id') IS DISTINCT FROM NEW.actor_id
           OR (body#>>'{execution_context,actor_id}') IS DISTINCT FROM NEW.actor_id
           OR (body->>'investigation_id') IS DISTINCT FROM owner.investigation_id
           OR (body->>'round_id') IS DISTINCT FROM owner.round_id
           OR (body->>'action') IS DISTINCT FROM 'investigation:continue'
        THEN RAISE EXCEPTION 'attempt actor attribution is invalid'; END IF;
        RETURN NEW;
    END $$""")
    op.execute("CREATE TRIGGER attempt_actor BEFORE INSERT ON investigation_attempts "
               "FOR EACH ROW EXECUTE FUNCTION ledger_attempt_actor_check()")
    body = PAYLOAD_CHECK_V1.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    body = body.replace("NEW.schema_version <> 1 OR (body->>'schema_version') IS DISTINCT FROM '1'",
                        "NEW.schema_version NOT IN (1,2) OR (body->>'schema_version') IS DISTINCT FROM NEW.schema_version::text")
    op.execute(body)


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM investigation_operations WHERE schema_version=2 OR created_by IS NOT NULL)
           OR EXISTS(SELECT 1 FROM investigation_attempts WHERE actor_id IS NOT NULL)
        THEN RAISE EXCEPTION 'cannot discard runtime identity audit history'; END IF;
    END $$""")
    op.execute("DROP TRIGGER attempt_actor ON investigation_attempts")
    op.execute("DROP FUNCTION ledger_attempt_actor_check()")
    op.execute(PAYLOAD_CHECK_V1.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1))
    op.drop_constraint("ck_ledger_attempt_actor", "investigation_attempts", type_="check")
    for column in ("runtime_digest", "runtime_payload", "actor_id"):
        op.drop_column("investigation_attempts", column)
    op.drop_constraint("ck_ledger_operation_schema", "investigation_operations", type_="check")
    op.create_check_constraint("ck_ledger_operation_schema", "investigation_operations", "schema_version = 1")
    op.drop_column("investigation_operations", "created_by")

# Frozen v1 function body; never import or modify 0004 at migration runtime.
PAYLOAD_CHECK_V1 = """CREATE FUNCTION ledger_payload_check() RETURNS trigger LANGUAGE plpgsql AS $$
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
    END $$"""
