from sqlalchemy import String

from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow


def test_knowledge_chunk_table_has_frozen_columns_constraints_and_indexes() -> None:
    table = KnowledgeChunkRow.__table__

    assert list(table.columns.keys()) == [
        "chunk_id",
        "document_id",
        "document_version",
        "document_type",
        "title",
        "section_path",
        "content",
        "model_id",
        "source_uri",
        "page_start",
        "page_end",
        "ingestion_status",
        "created_at",
        "updated_at",
        "ready_at",
    ]
    assert table.primary_key.columns.keys() == ["chunk_id"]
    assert isinstance(table.c.source_uri.type, String)
    assert table.c.source_uri.type.length == 2048
    assert table.c.source_uri.nullable is False
    assert table.c.page_start.nullable is True
    assert table.c.page_end.nullable is True
    assert table.c.ingestion_status.server_default is not None

    constraint_names = {constraint.name for constraint in table.constraints}
    assert "ck_knowledge_chunks_ingestion_status" in constraint_names
    assert "ck_knowledge_chunks_page_range" in constraint_names
    assert {index.name for index in table.indexes} == {
        "ix_knowledge_chunks_status_chunk_id",
        "ix_knowledge_chunks_document_version",
    }


def test_ingestion_status_is_limited_to_pending_and_ready() -> None:
    assert list(IngestionStatus) == [
        IngestionStatus.PENDING,
        IngestionStatus.READY,
    ]
