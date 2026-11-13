"""Single-entry decisions, confirmation and invocation audit; preserve old chat history."""
from alembic import op
import sqlalchemy as sa
revision = "20261112_0017"
down_revision = "20261110_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_chat_action", "chat_turns", type_="check")
    op.create_check_constraint("ck_chat_action", "chat_turns", "action IN ('AUTO','CHAT','READ_INVESTIGATION','START_INVESTIGATION','ENTRY_DECISION')")
    op.execute("""
CREATE TABLE chat_entry_decisions (
	turn_id VARCHAR(128) NOT NULL, 
	request_payload TEXT NOT NULL, 
	decision_payload TEXT NOT NULL, 
	decision_digest VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	invocation_id VARCHAR(128), 
	investigation_id VARCHAR(128), 
	confirmed BOOLEAN, 
	confirmation_actor VARCHAR(128), 
	confirmed_at TIMESTAMP WITH TIME ZONE, 
	started_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (turn_id), 
	FOREIGN KEY(turn_id) REFERENCES chat_turns (turn_id), 
	CONSTRAINT ck_entry_status CHECK (status IN ('ANSWERED','REJECTED','PENDING_CONFIRMATION','READY','STARTED','SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')), 
	CONSTRAINT ck_entry_confirmation CHECK ((confirmed IS NULL AND confirmation_actor IS NULL AND confirmed_at IS NULL) OR (confirmed IS NOT NULL AND confirmation_actor IS NOT NULL AND confirmed_at IS NOT NULL)), 
	CONSTRAINT ck_entry_invocation CHECK ((status='ANSWERED' AND invocation_id IS NULL) OR (status<>'ANSWERED' AND invocation_id IS NOT NULL)), 
	CONSTRAINT ck_entry_started CHECK ((status IN ('STARTED','SUCCEEDED','FAILED','UNCERTAIN') AND started_at IS NOT NULL) OR (status NOT IN ('STARTED','SUCCEEDED','FAILED','UNCERTAIN') AND started_at IS NULL)), 
	CONSTRAINT ck_entry_cancelled CHECK (status <> 'CANCELLED' OR confirmed IS FALSE), 
	UNIQUE (invocation_id)
)

""")
    op.execute("""
    CREATE FUNCTION protect_entry_decision() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE parent_status text; parent_action text; tool text;
    BEGIN
      IF TG_OP='DELETE' THEN RAISE EXCEPTION 'entry history is immutable'; END IF;
      tool := NEW.decision_payload::jsonb ->> 'name';
      IF TG_OP='INSERT' THEN
        SELECT status, action INTO parent_status, parent_action FROM chat_turns WHERE turn_id=NEW.turn_id FOR UPDATE;
        IF parent_status IS DISTINCT FROM 'STARTED' OR parent_action IS DISTINCT FROM 'ENTRY_DECISION'
          THEN RAISE EXCEPTION 'entry decision requires owned active entry turn'; END IF;
        IF NEW.status NOT IN ('ANSWERED','REJECTED','READY','PENDING_CONFIRMATION') OR NEW.confirmed IS NOT NULL
          THEN RAISE EXCEPTION 'invalid initial decision state'; END IF;
        IF tool='start_investigation' AND NEW.status NOT IN ('PENDING_CONFIRMATION','REJECTED')
          THEN RAISE EXCEPTION 'start proposal requires confirmation'; END IF;
        IF tool='get_investigation_progress' AND NEW.status NOT IN ('READY','REJECTED')
          THEN RAISE EXCEPTION 'invalid progress proposal state'; END IF;
        IF tool IS NULL AND NEW.status<>'ANSWERED' THEN RAISE EXCEPTION 'answer cannot invoke'; END IF;
        IF tool IS NOT NULL AND tool NOT IN ('start_investigation','get_investigation_progress')
          THEN RAISE EXCEPTION 'unknown entry tool'; END IF;
      ELSE
        IF OLD.status IN ('ANSWERED','REJECTED','SUCCEEDED','FAILED','UNCERTAIN','CANCELLED')
          THEN RAISE EXCEPTION 'entry terminal state is immutable'; END IF;
        IF ROW(NEW.turn_id,NEW.request_payload,NEW.decision_payload,NEW.decision_digest,NEW.invocation_id,NEW.investigation_id,NEW.created_at)
          IS DISTINCT FROM ROW(OLD.turn_id,OLD.request_payload,OLD.decision_payload,OLD.decision_digest,OLD.invocation_id,OLD.investigation_id,OLD.created_at)
          THEN RAISE EXCEPTION 'entry identity and input are immutable'; END IF;
        IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at
          THEN RAISE EXCEPTION 'invocation start time is immutable'; END IF;
        IF OLD.confirmed IS NOT NULL AND ROW(NEW.confirmed,NEW.confirmation_actor,NEW.confirmed_at)
          IS DISTINCT FROM ROW(OLD.confirmed,OLD.confirmation_actor,OLD.confirmed_at)
          THEN RAISE EXCEPTION 'confirmation is immutable'; END IF;
        IF NOT ((OLD.status='PENDING_CONFIRMATION' AND NEW.status IN ('READY','CANCELLED'))
             OR (OLD.status='READY' AND NEW.status='STARTED')
             OR (OLD.status='STARTED' AND NEW.status IN ('SUCCEEDED','FAILED','UNCERTAIN')))
          THEN RAISE EXCEPTION 'invalid entry transition'; END IF;
      END IF;
      IF tool IS NOT NULL AND NEW.investigation_id IS NULL THEN RAISE EXCEPTION 'missing stable case identity'; END IF;
      IF tool='start_investigation' AND NEW.status IN ('READY','STARTED','SUCCEEDED','FAILED','UNCERTAIN') AND NEW.confirmed IS DISTINCT FROM TRUE
        THEN RAISE EXCEPTION 'start requires explicit confirmation'; END IF;
      IF tool='get_investigation_progress' AND NEW.confirmed IS NOT NULL
        THEN RAISE EXCEPTION 'progress is not a confirmable action'; END IF;
      IF tool='start_investigation' AND NEW.confirmed IS NOT NULL AND NEW.confirmation_actor IS DISTINCT FROM
        (SELECT actor_id FROM chat_turns WHERE turn_id=NEW.turn_id)
        THEN RAISE EXCEPTION 'confirmation actor mismatch'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER protect_entry_decision BEFORE INSERT OR UPDATE OR DELETE ON chat_entry_decisions
    FOR EACH ROW EXECUTE FUNCTION protect_entry_decision();

    CREATE FUNCTION check_entry_finalization() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE t chat_turns%ROWTYPE; d chat_entry_decisions%ROWTYPE;
    BEGIN
      SELECT * INTO t FROM chat_turns WHERE turn_id=NEW.turn_id;
      IF t.action <> 'ENTRY_DECISION' THEN RETURN NULL; END IF;
      SELECT * INTO d FROM chat_entry_decisions WHERE turn_id=NEW.turn_id;
      IF NOT FOUND THEN
        IF t.status='SUCCEEDED' THEN RAISE EXCEPTION 'successful entry turn requires decision'; END IF;
        RETURN NULL;
      END IF;
      IF (d.status IN ('ANSWERED','REJECTED','SUCCEEDED','CANCELLED') AND t.status<>'SUCCEEDED')
         OR (d.status IN ('FAILED','UNCERTAIN') AND t.status<>'FAILED')
         OR (d.status IN ('PENDING_CONFIRMATION','READY','STARTED') AND t.status<>'STARTED')
        THEN RAISE EXCEPTION 'entry result and turn status must commit atomically'; END IF;
      RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER check_entry_turn_finalization AFTER INSERT OR UPDATE ON chat_turns
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_finalization();
    CREATE CONSTRAINT TRIGGER check_entry_decision_finalization AFTER INSERT OR UPDATE ON chat_entry_decisions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_finalization();
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM chat_entry_decisions) OR EXISTS(SELECT 1 FROM chat_turns WHERE action='ENTRY_DECISION')")):
        raise RuntimeError("Entry history exists; downgrade requires explicit data policy")
    op.execute("DROP TRIGGER check_entry_decision_finalization ON chat_entry_decisions")
    op.execute("DROP TRIGGER check_entry_turn_finalization ON chat_turns")
    op.execute("DROP FUNCTION check_entry_finalization()")
    op.execute("DROP TRIGGER protect_entry_decision ON chat_entry_decisions")
    op.execute("DROP FUNCTION protect_entry_decision()")
    op.drop_table("chat_entry_decisions")
    op.drop_constraint("ck_chat_action", "chat_turns", type_="check")
    op.create_check_constraint("ck_chat_action", "chat_turns", "action IN ('AUTO','CHAT','READ_INVESTIGATION','START_INVESTIGATION')")
