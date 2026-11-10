"""Persist conversations and immutable completed turns."""
from alembic import op
revision = "20261110_0015"
down_revision = "20261110_0014"
branch_labels = None
depends_on = None

def upgrade():
    op.execute("""
CREATE TABLE chat_conversations (
	conversation_id VARCHAR(128) NOT NULL, 
	owner_id VARCHAR(128) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (conversation_id), 
	CONSTRAINT uq_chat_owner UNIQUE (conversation_id, owner_id)
)

""")
    op.execute("""
CREATE TABLE chat_turns (
	turn_id VARCHAR(128) NOT NULL, 
	conversation_id VARCHAR(128) NOT NULL, 
	actor_id VARCHAR(128) NOT NULL, 
	request_digest VARCHAR(64) NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	user_text TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	response_payload TEXT, 
	response_digest VARCHAR(64), 
	memory_payload TEXT, 
	memory_digest VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	ended_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (turn_id), 
	FOREIGN KEY(conversation_id, actor_id) REFERENCES chat_conversations (conversation_id, owner_id), 
	CONSTRAINT ck_chat_action CHECK (action IN ('CHAT','READ_INVESTIGATION','START_INVESTIGATION')), 
	CONSTRAINT ck_chat_status CHECK ((status='STARTED' AND response_payload IS NULL AND response_digest IS NULL AND ended_at IS NULL AND memory_payload IS NULL AND memory_digest IS NULL) OR (status IN ('SUCCEEDED','FAILED') AND response_payload IS NOT NULL AND response_digest IS NOT NULL AND ended_at IS NOT NULL)), 
	CONSTRAINT ck_chat_memory CHECK ((memory_payload IS NULL AND memory_digest IS NULL) OR (status='SUCCEEDED' AND memory_payload IS NOT NULL AND memory_digest IS NOT NULL))
)

""")
    op.execute("CREATE UNIQUE INDEX uq_chat_active ON chat_turns (conversation_id) WHERE status='STARTED'")
    op.execute("""
    CREATE FUNCTION protect_chat_turn() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'chat history is immutable'; END IF;
      IF OLD.status <> 'STARTED' OR
        ROW(NEW.turn_id,NEW.conversation_id,NEW.actor_id,NEW.request_digest,NEW.action,NEW.user_text,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.turn_id,OLD.conversation_id,OLD.actor_id,OLD.request_digest,OLD.action,OLD.user_text,OLD.created_at)
      THEN RAISE EXCEPTION 'chat history is immutable'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER protect_chat_turn BEFORE UPDATE OR DELETE ON chat_turns
    FOR EACH ROW EXECUTE FUNCTION protect_chat_turn();
    """)

def downgrade():
    op.execute("DROP TRIGGER protect_chat_turn ON chat_turns")
    op.execute("DROP FUNCTION protect_chat_turn()")
    op.drop_table("chat_turns")
    op.drop_table("chat_conversations")
