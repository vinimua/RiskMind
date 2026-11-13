"""Persist auto intent decisions before dispatch."""
from alembic import op
import sqlalchemy as sa
revision = "20261110_0016"
down_revision = "20261110_0015"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("chat_turns", sa.Column("intent_payload", sa.Text()))
    op.add_column("chat_turns", sa.Column("intent_digest", sa.String(64)))
    op.drop_constraint("ck_chat_action", "chat_turns", type_="check")
    op.create_check_constraint("ck_chat_action", "chat_turns", "action IN ('AUTO','CHAT','READ_INVESTIGATION','START_INVESTIGATION')")
    op.create_check_constraint("ck_chat_intent", "chat_turns", "(intent_payload IS NULL AND intent_digest IS NULL) OR (action='AUTO' AND intent_payload IS NOT NULL AND intent_digest IS NOT NULL)")
    op.execute("""
    CREATE FUNCTION protect_chat_intent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF OLD.intent_payload IS NOT NULL AND ROW(NEW.intent_payload, NEW.intent_digest)
         IS DISTINCT FROM ROW(OLD.intent_payload, OLD.intent_digest)
      THEN RAISE EXCEPTION 'intent decision is immutable'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER protect_chat_intent BEFORE UPDATE ON chat_turns
    FOR EACH ROW EXECUTE FUNCTION protect_chat_intent();
    """)

def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM chat_turns WHERE action='AUTO' OR intent_payload IS NOT NULL)")):
        raise RuntimeError("Intent history exists; downgrade requires explicit data policy")
    op.drop_constraint("ck_chat_action", "chat_turns", type_="check")
    op.create_check_constraint("ck_chat_action", "chat_turns", "action IN ('CHAT','READ_INVESTIGATION','START_INVESTIGATION')")
    op.execute("DROP TRIGGER protect_chat_intent ON chat_turns")
    op.execute("DROP FUNCTION protect_chat_intent()")
    op.drop_constraint("ck_chat_intent", "chat_turns", type_="check")
    op.drop_column("chat_turns", "intent_digest")
    op.drop_column("chat_turns", "intent_payload")
