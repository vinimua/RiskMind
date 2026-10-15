import pytest
from pydantic import ValidationError

from risk_platform.knowledge.contracts import (
    KnowledgeDocumentType,
    KnowledgeFilters,
    KnowledgeHit,
    KnowledgeRetrievalResult,
    KnowledgeSearchRequest,
    KnowledgeSourceRef,
    RetrievalStatus,
)


def _hit(
    *,
    document_id: str = "document-001",
    chunk_id: str = "chunk-001",
    rank: int = 1,
    model_id: str | None = "credit_v3",
    case_id: str | None = "CASE-2026-018",
) -> KnowledgeHit:
    return KnowledgeHit(
        source=KnowledgeSourceRef(
            document_id=document_id,
            document_version="v1",
            chunk_id=chunk_id,
        ),
        document_type=KnowledgeDocumentType.INCIDENT_REPORT,
        model_id=model_id,
        case_id=case_id,
        title="credit_v3 KS 下降调查",
        section_path="调查过程/标签成熟度",
        content="T+30 标签成熟后 KS 恢复。",
        rank=rank,
    )


def test_search_request_contains_only_current_retrieval_inputs() -> None:
    request = KnowledgeSearchRequest(
        query="检索 credit_v3 的历史 KS 下降案例",
        filters=KnowledgeFilters(
            document_types=[KnowledgeDocumentType.INCIDENT_REPORT],
            model_id="credit_v3",
            case_id="CASE-2026-018",
            document_version="v1",
        ),
        top_k=10,
    )

    assert set(KnowledgeSearchRequest.model_fields) == {"query", "filters", "top_k"}
    assert request.filters.model_id == "credit_v3"

    with pytest.raises(ValidationError):
        KnowledgeSearchRequest.model_validate(
            {**request.model_dump(), "strategy": "HYBRID"}
        )


def test_search_request_uses_an_independent_empty_filter_by_default() -> None:
    first = KnowledgeSearchRequest(query="first", top_k=5)
    second = KnowledgeSearchRequest(query="second", top_k=5)

    first.filters.document_types.append(KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT)

    assert second.filters.document_types == []


@pytest.mark.parametrize("top_k", [0, -1])
def test_search_request_rejects_non_positive_top_k(top_k: int) -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchRequest(query="credit_v3", top_k=top_k)


def test_search_request_rejects_an_empty_query() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchRequest(query="", top_k=5)


def test_search_request_rejects_a_whitespace_only_query() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSearchRequest(query="   ", top_k=5)


@pytest.mark.parametrize("field", ["document_id", "document_version", "chunk_id"])
def test_source_reference_rejects_whitespace_only_ids(field: str) -> None:
    values = {
        "document_id": "document-001",
        "document_version": "v1",
        "chunk_id": "chunk-001",
    }
    values[field] = "   "

    with pytest.raises(ValidationError):
        KnowledgeSourceRef.model_validate(values)


@pytest.mark.parametrize("field", ["model_id", "case_id", "document_version"])
def test_filters_reject_whitespace_only_ids(field: str) -> None:
    with pytest.raises(ValidationError):
        KnowledgeFilters.model_validate({field: "   "})


def test_knowledge_hit_has_one_source_reference_and_no_pipeline_scores() -> None:
    hit = _hit()

    assert set(KnowledgeHit.model_fields) == {
        "source",
        "document_type",
        "model_id",
        "case_id",
        "title",
        "section_path",
        "content",
        "rank",
    }
    assert hit.source.chunk_id == "chunk-001"
    assert hit.model_id == "credit_v3"
    assert hit.case_id == "CASE-2026-018"

    with pytest.raises(ValidationError):
        KnowledgeHit.model_validate({**hit.model_dump(), "dense_score": 0.91})


@pytest.mark.parametrize("field", ["model_id", "case_id", "title", "content"])
def test_knowledge_hit_rejects_whitespace_only_values(field: str) -> None:
    values = _hit().model_dump()
    values[field] = "   "

    with pytest.raises(ValidationError):
        KnowledgeHit.model_validate(values)


def test_knowledge_hit_allows_an_empty_section_path() -> None:
    values = _hit().model_dump()
    values["section_path"] = ""

    assert KnowledgeHit.model_validate(values).section_path == ""


def test_successful_result_can_express_no_relevant_knowledge() -> None:
    result = KnowledgeRetrievalResult(status=RetrievalStatus.SUCCEEDED)

    assert result.hits == []
    assert result.error is None


def test_retrieval_result_round_trips_ranked_hits_through_json() -> None:
    result = KnowledgeRetrievalResult(
        status=RetrievalStatus.SUCCEEDED,
        hits=[_hit(rank=1), _hit(chunk_id="chunk-002", rank=2)],
    )

    restored = KnowledgeRetrievalResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.hits[0].document_type is KnowledgeDocumentType.INCIDENT_REPORT
    assert restored.hits[0].model_id == "credit_v3"
    assert restored.hits[0].case_id == "CASE-2026-018"


def test_validation_does_not_strip_content() -> None:
    values = _hit().model_dump()
    values["content"] = "  T+30 标签成熟后 KS 恢复。  "

    hit = KnowledgeHit.model_validate(values)

    assert hit.content == "  T+30 标签成熟后 KS 恢复。  "


def test_result_rejects_same_chunk_id_from_different_documents() -> None:
    with pytest.raises(ValidationError):
        KnowledgeRetrievalResult(
            status=RetrievalStatus.SUCCEEDED,
            hits=[
                _hit(document_id="document-001", rank=1),
                _hit(document_id="document-002", rank=2),
            ],
        )


@pytest.mark.parametrize(
    "result",
    [
        {"status": RetrievalStatus.FAILED},
        {"status": RetrievalStatus.FAILED, "error": "timeout", "hits": [_hit()]},
        {"status": RetrievalStatus.SUCCEEDED, "error": "unexpected"},
        {"status": RetrievalStatus.SUCCEEDED, "hits": [_hit(rank=2)]},
        {
            "status": RetrievalStatus.SUCCEEDED,
            "hits": [_hit(rank=1), _hit(rank=2)],
        },
    ],
)
def test_retrieval_result_rejects_inconsistent_state(result: dict) -> None:
    with pytest.raises(ValidationError):
        KnowledgeRetrievalResult.model_validate(result)


def test_failed_result_keeps_a_simple_error_without_hits() -> None:
    result = KnowledgeRetrievalResult(
        status=RetrievalStatus.FAILED,
        error="Milvus request timed out",
    )

    assert result.hits == []
    assert result.error == "Milvus request timed out"
