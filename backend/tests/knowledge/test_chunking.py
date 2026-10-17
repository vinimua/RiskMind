from pathlib import Path

import pytest
from pydantic import ValidationError

from risk_platform.knowledge import chunking
from risk_platform.knowledge.chunking import (
    DocumentPage,
    KnowledgeChunk,
    KnowledgeDocument,
    chunk_document,
)
from risk_platform.knowledge.contracts import KnowledgeDocumentType


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _document(
    content: str,
    *,
    document_id: str = "MODEL-DEV-credit_v3",
    document_version: str = "v1",
    document_type: KnowledgeDocumentType = KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
    title: str = "credit_v3 报告",
    source_uri: str = "knowledge://fixtures/credit_v3.md",
    pages: tuple[DocumentPage, ...] | None = None,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=document_id,
        document_version=document_version,
        document_type=document_type,
        title=title,
        source_uri=source_uri,
        model_id="credit_v3",
        pages=pages or (DocumentPage(content=content),),
    )


def _chunk_v1(
    document: KnowledgeDocument,
    *,
    chunk_size: int = 400,
    overlap: int = 60,
    table_max_rows: int = 10,
) -> list[KnowledgeChunk]:
    return chunking._chunk_document_v1(
        document,
        chunk_size=chunk_size,
        overlap=overlap,
        table_max_rows=table_max_rows,
    )


def _fixture_document(
    fixture_name: str,
    document_type: KnowledgeDocumentType,
    document_id: str,
    title: str,
) -> KnowledgeDocument:
    path = PROJECT_ROOT / "docs" / "runbooks" / fixture_name
    return _document(
        path.read_text(encoding="utf-8"),
        document_id=document_id,
        document_type=document_type,
        title=title,
        source_uri=f"knowledge://fixtures/{fixture_name}",
    )


def test_title_is_document_title_and_section_path_is_normalized() -> None:
    document = _document(
        """# credit_v3 模型开发报告

报告概述。

## 1 数据分析

数据分析正文。

### 1.1 缺失值处理

缺失值处理正文。"""
    )

    chunks = chunk_document(document)

    assert all(item.title == document.title for item in chunks)
    assert [item.section_path for item in chunks] == [
        "",
        "数据分析",
        "数据分析 / 缺失值处理",
    ]


def test_public_entrypoint_does_not_accept_custom_scheme_parameters() -> None:
    with pytest.raises(TypeError):
        chunk_document(_document("# 标题\n\n正文。"), chunk_size=800)  # type: ignore[call-arg]


def test_chunk_id_contains_chunking_scheme_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document("# 标题\n\n同一份正文。")
    v1_ids = [item.chunk_id for item in chunk_document(document)]

    monkeypatch.setattr(chunking, "CHUNKING_SCHEME_VERSION", "v2")
    v2_ids = [item.chunk_id for item in chunk_document(document)]

    assert v1_ids != v2_ids


def test_short_paragraphs_are_not_over_fragmented() -> None:
    chunks = _chunk_v1(
        _document("# 背景\n\n第一段。\n\n第二段。"),
        chunk_size=100,
        overlap=10,
    )

    assert len(chunks) == 1
    assert chunks[0].content == "第一段。\n\n第二段。"


def test_prose_chunks_include_overlap_without_exceeding_final_limit() -> None:
    document = _document(
        "# 根因分析\n\n"
        "第一句话用于介绍异常。"
        "第二句话用于说明数据变化。"
        "第三句话用于给出调查结论。"
        "第四句话用于补充处置建议。"
    )

    chunks = _chunk_v1(document, chunk_size=40, overlap=10)

    assert len(chunks) >= 2
    assert all(len(item.content) <= 40 for item in chunks)


def test_complete_sentence_overlap_is_preserved_when_it_fits() -> None:
    document = _document("# 调查\n\n第一句。第二句。第三句。第四句。第五句。")

    chunks = _chunk_v1(document, chunk_size=16, overlap=5)

    assert len(chunks) >= 2
    assert all(len(item.content) <= 16 for item in chunks)
    assert chunks[0].content.endswith("第二句。")
    assert chunks[1].content.startswith("第二句。")


def test_table_chunks_repeat_header_and_obey_row_and_size_limits() -> None:
    document = _document(
        """# credit_v3 模型开发报告

## 1 模型评估

### 1.1 评估结果

| 指标 | 开发集 | 验证集 |
|---|---:|---:|
| KS | 0.45 | 0.41 |
| AUC | 0.79 | 0.77 |
| PSI | 0.03 | 0.08 |
| Recall | 0.72 | 0.69 |
| Precision | 0.66 | 0.63 |
"""
    )

    chunks = _chunk_v1(document, chunk_size=90, overlap=10, table_max_rows=2)

    assert len(chunks) == 3
    assert all(item.title == document.title for item in chunks)
    assert all(item.section_path == "模型评估 / 评估结果" for item in chunks)
    assert all(len(item.content) <= 90 for item in chunks)
    assert all(
        item.content.startswith("| 指标 | 开发集 | 验证集 |\n|---|---:|---:|")
        for item in chunks
    )


def test_table_with_one_oversized_row_fails_fast() -> None:
    document = _document(
        "# 报告\n\n| 指标 | 说明 |\n|---|---|\n| KS | " + "很长" * 40 + " |"
    )

    with pytest.raises(ValueError, match="table header plus one row"):
        _chunk_v1(document, chunk_size=80, overlap=10)


def test_prose_around_a_table_is_not_lost() -> None:
    document = _document(
        """# 评估结果

表格前的结果说明。

| 指标 | 值 |
|---|---:|
| KS | 0.41 |

表格后的结论说明。"""
    )

    chunks = chunk_document(document)

    assert sum("表格前的结果说明。" in item.content for item in chunks) == 1
    assert sum("| KS | 0.41 |" in item.content for item in chunks) == 1
    assert sum("表格后的结论说明。" in item.content for item in chunks) == 1


def test_code_fence_is_atomic_and_heading_inside_is_not_a_section() -> None:
    code = "```text\n# 这是代码块内容\ncredit_v3 -> score\n```"
    chunks = chunk_document(_document(f"# 模型架构\n\n{code}"))

    assert len(chunks) == 1
    assert chunks[0].section_path == ""
    assert chunks[0].content == code


def test_oversized_code_fence_fails_fast() -> None:
    code = "```text\n" + "x" * 80 + "\n```"

    with pytest.raises(ValueError, match="fenced code block"):
        _chunk_v1(_document(f"# 模型架构\n\n{code}"), chunk_size=40, overlap=10)


def test_pdf_page_range_and_stable_source_are_preserved() -> None:
    pages = (
        DocumentPage(page_number=7, content="# 调查过程\n\n第一页的调查内容。"),
        DocumentPage(page_number=8, content="第二页的调查结论。"),
    )
    document = _document(
        "unused",
        document_id="CASE-2026-018",
        document_type=KnowledgeDocumentType.INCIDENT_REPORT,
        source_uri="minio://riskmind/cases/CASE-2026-018.pdf",
        pages=pages,
    )

    chunks = chunk_document(document)

    assert len(chunks) == 1
    assert chunks[0].source_uri == document.source_uri
    assert (chunks[0].page_start, chunks[0].page_end) == (7, 8)


def test_local_file_path_is_not_a_stable_source_uri() -> None:
    with pytest.raises(ValidationError, match="stable non-file URI"):
        _document("# 标题\n\n正文。", source_uri=r"C:\\Downloads\\report.pdf")


def test_same_document_produces_stable_unique_chunk_ids() -> None:
    document = _document("# 概述\n\n第一段。\n\n第二段。\n\n第三段。")

    first = _chunk_v1(document, chunk_size=12, overlap=2)
    second = _chunk_v1(document, chunk_size=12, overlap=2)

    first_ids = [item.chunk_id for item in first]
    assert first_ids == [item.chunk_id for item in second]
    assert len(first_ids) == len(set(first_ids))


def test_document_version_changes_chunk_ids() -> None:
    content = "# 概述\n\ncredit_v3 模型背景。"

    v1 = chunk_document(_document(content, document_version="v1"))
    v2 = chunk_document(_document(content, document_version="v2"))

    assert [item.chunk_id for item in v1] != [item.chunk_id for item in v2]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"chunk_size": 0}, "chunk_size"),
        ({"chunk_size": 10, "overlap": 10}, "overlap"),
        ({"table_max_rows": 0}, "table_max_rows"),
    ],
)
def test_invalid_private_chunking_options_are_rejected(
    kwargs: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _chunk_v1(_document("# 标题\n\n正文。"), **kwargs)


def test_mixed_or_out_of_order_pdf_pages_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _document(
            "unused",
            pages=(
                DocumentPage(page_number=2, content="page two"),
                DocumentPage(page_number=1, content="page one"),
            ),
        )

    with pytest.raises(ValidationError):
        _document(
            "unused",
            pages=(
                DocumentPage(page_number=1, content="page one"),
                DocumentPage(content="unknown page"),
            ),
        )


@pytest.mark.parametrize(
    ("fixture_name", "document_type", "document_id", "title"),
    [
        (
            "credit_v3_model_development_report_fixture.md",
            KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
            "MODEL-DEV-credit_v3",
            "credit_v3 模型开发报告",
        ),
        (
            "credit_v3_incident_report_fixture.md",
            KnowledgeDocumentType.INCIDENT_REPORT,
            "CASE-2026-018",
            "credit_v3 模型异常调查报告",
        ),
    ],
)
def test_stage4_fixtures_obey_identity_size_and_noise_invariants(
    fixture_name: str,
    document_type: KnowledgeDocumentType,
    document_id: str,
    title: str,
) -> None:
    document = _fixture_document(fixture_name, document_type, document_id, title)

    first = chunk_document(document)
    second = chunk_document(document)

    assert first
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]
    assert len({item.chunk_id for item in first}) == len(first)
    assert all(item.title == document.title for item in first)
    assert all(len(item.content) <= chunking.DEFAULT_CHUNK_SIZE for item in first)
    searchable = "\n".join(
        f"{item.section_path}\n{item.content}" for item in first
    )
    for excluded in ("Synthetic Fixture", "Stage 4", "修订历史", "文档说明"):
        assert excluded not in searchable


def test_model_report_golden_ks_auc_table_is_preserved() -> None:
    document = _fixture_document(
        "credit_v3_model_development_report_fixture.md",
        KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        "MODEL-DEV-credit_v3",
        "credit_v3 模型开发报告",
    )

    assert any(
        item.section_path == "模型评估 / 评估结果"
        and "| KS |" in item.content
        and "| AUC |" in item.content
        for item in chunk_document(document)
    )


def test_incident_report_golden_business_knowledge_is_preserved() -> None:
    document = _fixture_document(
        "credit_v3_incident_report_fixture.md",
        KnowledgeDocumentType.INCIDENT_REPORT,
        "CASE-2026-018",
        "credit_v3 模型异常调查报告",
    )
    chunks = chunk_document(document)

    assert any(
        item.section_path == "根因与证据 / 根因结论"
        and "标签成熟度不足" in item.content
        for item in chunks
    )
    assert any(
        item.section_path
        == "调查过程 / 调查思路与检查项 / 检查项 2：模型与特征版本"
        and "未发现模型或特征版本错配" in item.content
        for item in chunks
    )
