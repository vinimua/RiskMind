"""Allow versioned registered-tool operations without rewriting historical rows."""
from alembic import op

revision = "20261114_0018"
down_revision = "20261112_0017"
branch_labels = None
depends_on = None


def replace_trigger_body(name, changes):
    """Emit SQL for both online migrations and offline deployment scripts."""
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    statements = [f"SELECT pg_get_functiondef({literal(name + '()')}::regprocedure) INTO body;"]
    for old, new in changes.items():
        statements.append(f"IF position({literal(old)} in body)=0 THEN RAISE EXCEPTION 'Unexpected trigger version'; END IF;")
        statements.append(f"body := replace(body, {literal(old)}, {literal(new)});")
    statements.append("EXECUTE body;")
    op.execute("DO $migration$ DECLARE body text; BEGIN\n" + "\n".join(statements) + "\nEND $migration$;")


def upgrade():
    op.drop_constraint("ck_ledger_operation_schema", "investigation_operations", type_="check")
    op.create_check_constraint("ck_ledger_operation_schema", "investigation_operations",
                               "schema_version IN (1,2,3) AND (schema_version = 1 OR created_by IS NOT NULL)")
    op.drop_constraint("ck_ledger_operation_type", "investigation_operations", type_="check")
    op.create_check_constraint("ck_ledger_operation_type", "investigation_operations",
        "(operation_type = 'OBSERVATION' AND task_id IS NULL) OR "
        "(operation_type IN ('QUERY_FORMULATION','KNOWLEDGE_EXECUTION','TOOL_EXECUTION') AND task_id IS NOT NULL)")
    op.drop_constraint("ck_ledger_result", "investigation_results", type_="check")
    op.create_check_constraint("ck_ledger_result", "investigation_results",
        "schema_version IN (1,2,3) AND NOT (execution_id IS NOT NULL AND observation_id IS NOT NULL)")
    # Extend the existing integrity trigger; keep digest, identity and exactly-one checks.
    changes = {
        "NEW.schema_version NOT IN (1,2)": "NEW.schema_version NOT IN (1,2,3)",
        "operation.operation_type = 'KNOWLEDGE_EXECUTION'": "operation.operation_type IN ('KNOWLEDGE_EXECUTION','TOOL_EXECUTION')",
        "NEW.schema_version <> 2": "NEW.schema_version NOT IN (2,3)",
        "IS DISTINCT FROM 'KNOWLEDGE_RETRIEVAL'":
            "IS DISTINCT FROM COALESCE(operation.payload::jsonb->>'capability','KNOWLEDGE_RETRIEVAL')",
    }
    replace_trigger_body("ledger_payload_check", changes)
    for function in ("ledger_attempt_actor_check", "planning_integrity"):
        version = "(body->>'schema_version') IS DISTINCT FROM '1'" if function == "ledger_attempt_actor_check" else "body->>'schema_version' IS DISTINCT FROM '1'"
        replace_trigger_body(function, {
            version: "(body->>'schema_version') NOT IN ('1','2')",
            "body#>>'{execution_context,actor_id}'":
                "CASE WHEN body->>'schema_version'='2' THEN body->>'actor_id' ELSE body#>>'{execution_context,actor_id}' END",
        })


def downgrade():
    # New immutable execution records cannot be dropped or silently reinterpreted.
    raise RuntimeError("Retire or migrate v3 tool records explicitly before downgrade")
