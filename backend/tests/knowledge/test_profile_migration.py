from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config


def migration_config(output=None):
    backend = Path(__file__).resolve().parents[2]
    config = Config(str(backend / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("path_separator", "os")
    return config


def test_profile_migration_generates_postgres_ddl_and_guards_downgrade(monkeypatch):
    monkeypatch.setenv("RISK_DATABASE_URL", "postgresql+psycopg://unused:unused@localhost/unused")
    output = StringIO()
    command.upgrade(migration_config(output), "head", sql=True)
    sql = output.getvalue()
    assert "DEFAULT 'baseline-v1' NOT NULL" in sql
    assert "PRIMARY KEY (chunk_id, index_profile)" in sql
    for table in ("knowledge_documents", "knowledge_builds", "knowledge_contexts",
                  "knowledge_context_links", "knowledge_collections"):
        assert f"CREATE TABLE {table}" in sql
    assert "WHERE purpose = 'INGESTION'" in sql
    assert "FOREIGN KEY(chunk_id, index_profile)" in sql
    output = StringIO()
    command.downgrade(migration_config(output), "20261024_0002:20261017_0001", sql=True)
    assert "cannot downgrade populated index profiles" in output.getvalue()
