import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.models import IngestionStatus, KnowledgeChunkRow
from risk_platform.knowledge.repository import (
    DocumentVersionConflictError,
    count_by_status,
    list_pending_chunks,
    mark_chunks_ready,
    stage_chunks,
)


class _Result:
    def __init__(self, *, rows=(), rowcount: int = 0, scalar: int = 0):
        self._rows = list(rows)
        self.rowcount = rowcount
        self._scalar = scalar

    def scalars(self):
        return self._rows

    def scalar_one(self) -> int:
        return self._scalar


def _session(result: _Result):
    session = Mock()
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _chunk(
    suffix: str,
    *,
    document_version: str = "v1",
) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=f"chunk-{suffix}",
        document_id="MODEL-DEV-credit_v3",
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        document_version=document_version,
        title="credit_v3 模型开发报告",
        section_path="模型评估 / 评估结果",
        content=f"第 {suffix} 个知识块",
        model_id="credit_v3",
        source_uri="minio://riskmind/models/credit_v3/v1.pdf",
        page_start=7,
        page_end=8,
    )


def _row(chunk: KnowledgeChunk, *, status: IngestionStatus) -> KnowledgeChunkRow:
    return KnowledgeChunkRow(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        document_type=chunk.document_type.value,
        title=chunk.title,
        section_path=chunk.section_path,
        content=chunk.content,
        model_id=chunk.model_id,
        source_uri=chunk.source_uri,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        ingestion_status=status.value,
    )


def test_stage_chunks_adds_one_document_as_pending_without_committing() -> None:
    chunks = [_chunk("001"), _chunk("002")]
    session = _session(_Result())

    staged = asyncio.run(stage_chunks(session, chunks))

    assert staged == 2
    rows = session.add_all.call_args.args[0]
    assert [row.chunk_id for row in rows] == ["chunk-001", "chunk-002"]
    assert all(row.ingestion_status == IngestionStatus.PENDING.value for row in rows)
    session.flush.assert_awaited_once_with()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_identical_document_version_is_idempotent_and_ready_does_not_regress() -> None:
    chunks = [_chunk("001"), _chunk("002")]
    existing = [
        _row(chunks[0], status=IngestionStatus.READY),
        _row(chunks[1], status=IngestionStatus.PENDING),
    ]
    session = _session(_Result(rows=reversed(existing)))

    staged = asyncio.run(stage_chunks(session, chunks))

    assert staged == 0
    session.add_all.assert_not_called()
    session.flush.assert_not_awaited()
    assert existing[0].ingestion_status == IngestionStatus.READY.value
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("chunk_id", "chunk-changed"),
        ("document_type", KnowledgeDocumentType.INCIDENT_REPORT),
        ("title", "不同标题"),
        ("section_path", "不同章节"),
        ("content", "不同正文"),
        ("model_id", None),
        ("source_uri", "minio://riskmind/models/credit_v3/other.pdf"),
        ("page_start", 9),
        ("page_end", 10),
    ],
)
def test_existing_document_version_rejects_any_business_field_change(
    field: str,
    changed: object,
) -> None:
    original = _chunk("001")
    incoming = original.model_copy(update={field: changed})
    session = _session(
        _Result(rows=[_row(original, status=IngestionStatus.READY)])
    )

    with pytest.raises(DocumentVersionConflictError, match="is immutable"):
        asyncio.run(stage_chunks(session, [incoming]))

    session.add_all.assert_not_called()
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_stage_chunks_rejects_empty_mixed_or_duplicate_input() -> None:
    session = _session(_Result())

    with pytest.raises(ValueError, match="at least one"):
        asyncio.run(stage_chunks(session, []))
    with pytest.raises(ValueError, match="one document version"):
        asyncio.run(stage_chunks(session, [_chunk("001"), _chunk("002", document_version="v2")]))
    with pytest.raises(ValueError, match="must be unique"):
        asyncio.run(stage_chunks(session, [_chunk("001"), _chunk("001")]))

    session.execute.assert_not_awaited()


def test_different_document_version_uses_an_independent_stage_query() -> None:
    session = _session(_Result())

    assert asyncio.run(stage_chunks(session, [_chunk("v2", document_version="v2")])) == 1
    statement = session.execute.await_args.args[0]
    parameters = statement.compile().params

    assert "MODEL-DEV-credit_v3" in parameters.values()
    assert "v2" in parameters.values()


def test_list_pending_chunks_is_limited_and_does_not_commit() -> None:
    row = _row(_chunk("001"), status=IngestionStatus.PENDING)
    session = _session(_Result(rows=[row]))

    assert asyncio.run(list_pending_chunks(session, limit=32)) == [row]
    assert session.execute.await_count == 1
    session.commit.assert_not_awaited()

    with pytest.raises(ValueError, match="positive"):
        asyncio.run(list_pending_chunks(session, limit=0))


def test_mark_ready_and_count_do_not_commit() -> None:
    mark_session = _session(_Result(rowcount=2))
    count_session = _session(_Result(scalar=2))

    assert asyncio.run(mark_chunks_ready(mark_session, ["a", "b", "a"])) == 2
    assert asyncio.run(mark_chunks_ready(mark_session, [])) == 0
    assert asyncio.run(count_by_status(count_session, IngestionStatus.READY)) == 2

    assert mark_session.execute.await_count == 1
    mark_session.commit.assert_not_awaited()
    count_session.commit.assert_not_awaited()
