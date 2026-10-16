from pathlib import Path

import pytest
from pydantic import ValidationError

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
    pages: tuple[DocumentPage, ...] | None = None,
) -> KnowledgeDocument:
    return KnowledgeDocument(
        document_id=document_id,
        document_version=document_version,
        document_type=document_type,
        title="credit_v3 报告",
        source_uri="docs/credit_v3.md",
        model_id="credit_v3",
        pages=pages or (DocumentPage(content=content),),
    )


def test_headings_create_titles_and_full_section_paths() -> None:
    document = _document(
        """# 模型开发报告

报告概述。

## 数据分析

数据分析正文。

### 缺失值处理

缺失值处理正文。
"""
    )

    chunks = chunk_document(document)

    assert [(chunk.title, chunk.section_path) for chunk in chunks] == [
        ("模型开发报告", "模型开发报告"),
        ("数据分析", "模型开发报告 / 数据分析"),
        ("缺失值处理", "模型开发报告 / 数据分析 / 缺失值处理"),
    ]


def test_short_paragraphs_in_one_section_are_not_over_fragmented() -> None:
    document = _document("# 背景\n\n第一段。\n\n第二段。")

    chunks = chunk_document(document, chunk_size=100, overlap=10)

    assert len(chunks) == 1
    assert chunks[0].content == "第一段。\n\n第二段。"


def test_long_chinese_content_uses_sentence_overlap() -> None:
    document = _document(
        "# 根因分析\n\n"
        "第一句用于介绍异常。"
        "第二句用于说明数据。"
        "第三句用于给出结论。"
    )

    chunks = chunk_document(document, chunk_size=24, overlap=12)

    assert len(chunks) >= 2
    assert chunks[1].content.startswith("第二句用于说明数据。")


def test_table_chunks_repeat_header_and_keep_section_title() -> None:
    document = _document(
        """# 模型评估

## 评估结果

| 指标 | 开发集 | 验证集 |
|---|---:|---:|
| KS | 0.45 | 0.41 |
| AUC | 0.79 | 0.77 |
| PSI | 0.03 | 0.08 |
| Recall | 0.72 | 0.69 |
| Precision | 0.66 | 0.63 |
"""
    )

    chunks = chunk_document(document, table_max_rows=2)

    assert len(chunks) == 3
    assert all(chunk.title == "评估结果" for chunk in chunks)
    assert all(chunk.content.startswith("| 指标 | 开发集 | 验证集 |\n|---|---:|---:|") for chunk in chunks)
    assert sum("| KS |" in chunk.content for chunk in chunks) == 1
    assert sum("| Precision |" in chunk.content for chunk in chunks) == 1


def test_prose_around_a_table_is_not_lost() -> None:
    document = _document(
        """# 评估结果

表格前的结果说明。

| 指标 | 值 |
|---|---:|
| KS | 0.41 |

表格后的结论说明。
"""
    )

    chunks = chunk_document(document)

    assert sum("表格前的结果说明。" in chunk.content for chunk in chunks) == 1
    assert sum("| KS | 0.41 |" in chunk.content for chunk in chunks) == 1
    assert sum("表格后的结论说明。" in chunk.content for chunk in chunks) == 1


def test_pdf_page_range_and_source_are_preserved_across_pages() -> None:
    pages = (
        DocumentPage(page_number=7, content="# 调查过程\n\n第一页的调查内容。"),
        DocumentPage(page_number=8, content="第二页的调查结论。"),
    )
    document = _document(
        "unused",
        document_id="CASE-2026-018",
        document_type=KnowledgeDocumentType.INCIDENT_REPORT,
        pages=pages,
    )

    chunks = chunk_document(document)

    assert len(chunks) == 1
    assert chunks[0].source_uri == "docs/credit_v3.md"
    assert (chunks[0].page_start, chunks[0].page_end) == (7, 8)


def test_same_document_produces_stable_unique_chunk_ids() -> None:
    document = _document("# 概述\n\n第一段。\n\n第二段。\n\n第三段。")

    first = chunk_document(document, chunk_size=8, overlap=2)
    second = chunk_document(document, chunk_size=8, overlap=2)

    first_ids = [chunk.chunk_id for chunk in first]
    assert first_ids == [chunk.chunk_id for chunk in second]
    assert len(first_ids) == len(set(first_ids))


def test_document_version_changes_chunk_ids() -> None:
    content = "# 概述\n\ncredit_v3 模型背景。"

    v1 = chunk_document(_document(content, document_version="v1"))
    v2 = chunk_document(_document(content, document_version="v2"))

    assert [chunk.chunk_id for chunk in v1] != [chunk.chunk_id for chunk in v2]


def test_heading_inside_code_fence_is_not_treated_as_a_section() -> None:
    document = _document(
        """# 模型架构

```text
# 这是代码块内容
credit_v3 -> score
```
"""
    )

    chunks = chunk_document(document)

    assert len(chunks) == 1
    assert chunks[0].title == "模型架构"
    assert "# 这是代码块内容" in chunks[0].content


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"chunk_size": 0}, "chunk_size"),
        ({"chunk_size": 10, "overlap": 10}, "overlap"),
        ({"table_max_rows": 0}, "table_max_rows"),
    ],
)
def test_invalid_chunking_options_are_rejected(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        chunk_document(_document("# 标题\n\n正文。"), **kwargs)


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
    ("fixture_name", "document_type", "document_id"),
    [
        (
            "credit_v3_model_development_report_fixture.md",
            KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
            "MODEL-DEV-credit_v3",
        ),
        (
            "credit_v3_incident_report_fixture.md",
            KnowledgeDocumentType.INCIDENT_REPORT,
            "CASE-2026-018",
        ),
    ],
)
def test_stage4_fixture_can_be_chunked_deterministically(
    fixture_name: str,
    document_type: KnowledgeDocumentType,
    document_id: str,
) -> None:
    path = PROJECT_ROOT / "docs" / "runbooks" / fixture_name
    content = path.read_text(encoding="utf-8")
    document = _document(
        content,
        document_id=document_id,
        document_type=document_type,
    )

    first = chunk_document(document)
    second = chunk_document(document)

    assert first
    assert all(isinstance(chunk, KnowledgeChunk) for chunk in first)
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert len({chunk.chunk_id for chunk in first}) == len(first)
    assert all(chunk.content.strip() for chunk in first)
