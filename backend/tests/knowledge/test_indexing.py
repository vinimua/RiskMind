from pathlib import Path

import pytest
from pydantic import ValidationError

from risk_platform.knowledge.chunking import (
    DocumentPage,
    KnowledgeDocument,
    chunk_document,
)
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.indexing import (
    BaselineIndexBuildStrategy,
    IndexBuildStrategy,
    PreparedKnowledge,
)


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(params=["model", "incident", "numbered-pages"])
def document(request: pytest.FixtureRequest) -> KnowledgeDocument:
    incident = request.param == "incident"
    if request.param == "numbered-pages":
        pages = (
            DocumentPage(
                page_number=7,
                content="# Report\n\n## Findings\n\n" + "Evidence. " * 80,
            ),
            DocumentPage(
                page_number=8,
                content="Further evidence.\n\n## Conclusion\n\nResolved.",
            ),
        )
    else:
        fixture_name = (
            "credit_v3_incident_report_fixture.md" if incident
            else "credit_v3_model_development_report_fixture.md"
        )
        path = PROJECT_ROOT / "docs" / "runbooks" / fixture_name
        pages = (DocumentPage(content=path.read_text(encoding="utf-8")),)
    return KnowledgeDocument(
        document_id="CASE-2026-018" if incident else "MODEL-DEV-credit_v3",
        document_version="v1",
        document_type=(
            KnowledgeDocumentType.INCIDENT_REPORT if incident
            else KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT
        ),
        title="credit_v3 report",
        source_uri="knowledge://fixtures/credit_v3",
        model_id="credit_v3",
        pages=pages,
    )


def test_baseline_preserves_all_chunks_in_order(document: KnowledgeDocument) -> None:
    strategy: IndexBuildStrategy = BaselineIndexBuildStrategy()
    expected = chunk_document(document)

    prepared = strategy.build(document)

    assert strategy.name == "baseline-v1"
    assert len(prepared.search_chunks) == len(expected) > 1
    assert prepared.search_chunks == tuple(expected)
    assert [chunk.chunk_id for chunk in prepared.search_chunks] == [
        chunk.chunk_id for chunk in expected
    ]
    # Full serialized equality includes content, section_path and all provenance:
    # document identity/version/type, model, source URI and page range.
    assert [chunk.model_dump() for chunk in prepared.search_chunks] == [
        chunk.model_dump() for chunk in expected
    ]


def test_baseline_build_is_deterministic(document: KnowledgeDocument) -> None:
    strategy = BaselineIndexBuildStrategy()

    assert strategy.build(document) == strategy.build(document)
    assert strategy.build(document) == BaselineIndexBuildStrategy().build(document)


def test_prepared_knowledge_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as error:
        PreparedKnowledge(search_chunks=(), parents=())

    assert error.value.errors()[0]["type"] == "extra_forbidden"


def test_prepared_knowledge_is_frozen(document: KnowledgeDocument) -> None:
    prepared = BaselineIndexBuildStrategy().build(document)

    assert isinstance(prepared.search_chunks, tuple)
    with pytest.raises(ValidationError) as error:
        prepared.search_chunks = ()

    assert error.value.errors()[0]["type"] == "frozen_instance"


def test_baseline_preserves_empty_output(document: KnowledgeDocument) -> None:
    document = document.model_copy(
        update={"pages": (DocumentPage(content="# Title"),)}
    )

    assert BaselineIndexBuildStrategy().build(document).search_chunks == tuple(
        chunk_document(document)
    ) == ()


def test_baseline_propagates_chunking_failure(document: KnowledgeDocument) -> None:
    document = document.model_copy(update={
        "pages": (DocumentPage(content="```text\n" + "x" * 401 + "\n```"),),
    })

    with pytest.raises(ValueError, match="fenced code block exceeds chunk_size"):
        BaselineIndexBuildStrategy().build(document)
