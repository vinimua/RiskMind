from pathlib import Path

import pytest
from pydantic import ValidationError

from risk_platform.knowledge import chunking
from risk_platform.knowledge.chunking import DocumentPage, KnowledgeDocument
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.indexing import (
    BaselineIndexBuildStrategy, KnowledgeContextLink, KnowledgeContextUnit,
    ParentChildIndexBuildStrategy, PreparedKnowledge,
)
from risk_platform.knowledge.indexing import parent_child


def document(content="", *, pages=None, version="v1"):
    return KnowledgeDocument(
        document_id="REPORT-1", document_version=version,
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        title="Report", model_id="credit_v3", source_uri="knowledge://reports/1",
        pages=pages or (DocumentPage(content=content),),
    )


def build(content):
    return ParentChildIndexBuildStrategy().build(document(content))


def test_short_sections_hierarchy_exclusion_and_fenced_heading():
    result = build(
        "# Title\nIntro.\n## 1 Analysis\nBody.\n### 1.1 Detail\n"
        "```text\n# Not a heading\n```\n## 2 目录\nExcluded.\n"
        "### Descendant\nAlso excluded.\n## 3 Conclusion\nDone."
    )
    assert [p.section_path for p in result.context_units] == [
        "", "Analysis", "Analysis / Detail", "Conclusion",
    ]
    assert [p.content for p in result.context_units] == [
        "Intro.", "Body.", "```text\n# Not a heading\n```", "Done.",
    ]


@pytest.mark.parametrize("length,count", [(1200, 1), (1600, 1), (1601, 2), (5000, 5)])
def test_parent_bounds_and_source_coverage(length, count):
    content = "x" * length
    result = build("# Title\n" + content)
    assert len(result.context_units) == count
    assert "".join(p.content for p in result.context_units) == content
    assert all(len(p.content) <= 1600 for p in result.context_units)
    assert all(len(c.content) <= 400 for c in result.search_chunks)


def test_long_section_preserves_natural_paragraph_boundaries():
    paragraphs = [str(i) * 500 + "。" for i in range(4)]
    result = build("# Title\n" + "\n\n".join(paragraphs))
    assert [p.content for p in result.context_units] == [
        "\n\n".join(paragraphs[:2]), "\n\n".join(paragraphs[2:]),
    ]


def test_parent_first_overlap_and_mapping(monkeypatch):
    # Unique short sentences force deterministic overlap without source repeats.
    content = "".join(f"证据{i:03d}已核实。" for i in range(220))
    original_split = chunking._split_prose
    seen = []

    def split(lines, size, overlap):
        seen.append("\n".join(line.text for line in lines))
        return original_split(lines, size, overlap)

    def forbidden(*args, **kwargs):
        raise AssertionError("baseline chunking must never build parents or children")

    monkeypatch.setattr(chunking, "_split_prose", split)
    monkeypatch.setattr(chunking, "chunk_document", forbidden)
    result = build("# Title\n" + content)
    assert seen == [p.content for p in result.context_units]
    assert "".join(p.content for p in result.context_units) == content
    children = {c.chunk_id: c for c in result.search_chunks}
    assert len(children) == len(result.search_chunks) == len(result.context_links)
    parents = {p.context_id: p for p in result.context_units}
    assert len(parents) == len(result.context_units)
    assert [link.chunk_id for link in result.context_links] == list(children)
    found_overlap = False
    for parent in result.context_units:
        links = [link for link in result.context_links if link.context_id == parent.context_id]
        assert [link.child_ordinal for link in links] == list(range(len(links)))
        linked_children = [children[link.chunk_id] for link in links]
        for child in linked_children:
            assert child.content in parent.content
            assert len(child.content) <= 400
        for left, right in zip(linked_children, linked_children[1:]):
            found_overlap |= any(left.content[-n:] == right.content[:n] for n in range(8, 61))
    assert found_overlap
    assert result == build("# Title\n" + content)


def test_identical_text_in_repeated_sections_has_distinct_parent_and_child_ids():
    result = build("# Title\n## Same\nRepeated.\n## Same\nRepeated.")
    assert len({p.context_id for p in result.context_units}) == 2
    assert len({c.chunk_id for c in result.search_chunks}) == 2
    assert result.context_links[0].child_ordinal == result.context_links[1].child_ordinal == 0


def test_numbered_pages_preserve_exact_child_spans_and_metadata():
    doc = document(pages=(
        DocumentPage(page_number=7, content="# Title\n## Results\n" + "甲。" * 170),
        DocumentPage(page_number=8, content="乙。" * 170),
        DocumentPage(page_number=9, content="丙。" * 170),
    ))
    result = ParentChildIndexBuildStrategy().build(doc)
    assert len(result.context_units) == 1
    assert (result.context_units[0].page_start, result.context_units[0].page_end) == (7, 9)
    assert result.search_chunks[0].page_start == result.search_chunks[0].page_end == 7
    assert result.search_chunks[-1].page_end == 9
    assert any(c.page_start < c.page_end for c in result.search_chunks)
    for item in (*result.context_units, *result.search_chunks):
        for field in ("document_id", "document_version", "document_type", "title", "model_id", "source_uri"):
            assert getattr(item, field) == getattr(doc, field)
        assert item.section_path == "Results"


@pytest.mark.parametrize("kind", ["code", "table"])
def test_oversized_parent_atomic_blocks_fail(kind):
    content = "```text\n" + "x" * 1601 + "\n```" if kind == "code" else (
        "| Key | Value |\n|---|---|\n" + "| x | y |\n" * 200
    )
    with pytest.raises(ValueError, match="atomic .* block exceeds parent hard max"):
        build("# Title\n" + content)


def test_child_atomic_code_limit_fails_without_silent_truncation():
    with pytest.raises(ValueError, match="fenced code block exceeds chunk_size"):
        build("# Title\n```text\n" + "x" * 401 + "\n```")


def test_tables_repeat_header_only_in_children_and_code_is_atomic():
    table = "| Key | Value |\n|---|---|\n" + "".join(f"| {i} | value |\n" for i in range(30))
    code = "```text\n# protected\nresult\n```"
    result = build("# Title\n" + table + "\n" + code)
    assert len(result.context_units) == 1
    assert result.context_units[0].content == table + "\n" + code
    assert result.context_units[0].content.count("| Key | Value |") == 1
    assert sum(c.content.startswith("| Key | Value |") for c in result.search_chunks) == 3
    assert result.search_chunks[-1].content == code
    assert all(len(c.content) <= 400 for c in result.search_chunks)


def test_document_and_scheme_versions_change_both_identities(monkeypatch):
    strategy = ParentChildIndexBuildStrategy()
    first = strategy.build(document("# Title\nText."))
    changed = strategy.build(document("# Title\nText.", version="v2"))
    monkeypatch.setattr(parent_child, "PARENT_CHILD_SCHEME_VERSION", "v2")
    scheme = strategy.build(document("# Title\nText."))
    assert len({r.context_units[0].context_id for r in (first, changed, scheme)}) == 3
    assert len({r.search_chunks[0].chunk_id for r in (first, changed, scheme)}) == 3


@pytest.mark.parametrize("mutation", [
    "missing-link", "duplicate-link", "unknown-child", "unknown-parent", "ordinal",
    "duplicate-child", "duplicate-parent", "provenance", "pages", "orphan-parent",
])
def test_invalid_mappings_rejected(mutation):
    result = build("# Title\n## First\nText.\n## Second\nOther.")
    data = result.model_dump()
    if mutation == "missing-link":
        data["context_links"] = data["context_links"][:-1]
    elif mutation == "duplicate-link":
        data["context_links"] += (data["context_links"][0],)
    elif mutation in ("unknown-child", "unknown-parent", "ordinal"):
        field, value = {
            "unknown-child": ("chunk_id", "unknown"),
            "unknown-parent": ("context_id", "unknown"),
            "ordinal": ("child_ordinal", 1),
        }[mutation]
        data["context_links"][0][field] = value
    elif mutation == "duplicate-child":
        data["search_chunks"] += (data["search_chunks"][0],)
    elif mutation == "duplicate-parent":
        data["context_units"] += (data["context_units"][0],)
    elif mutation == "provenance":
        data["search_chunks"][0]["document_version"] = "different"
    elif mutation == "pages":
        data["search_chunks"][0].update(page_start=1, page_end=2)
    else:
        parent = dict(data["context_units"][0], context_id="orphan")
        data["context_units"] += (parent,)
    with pytest.raises(ValidationError):
        PreparedKnowledge.model_validate(data)


def test_new_contracts_are_frozen_strict_and_validate_provenance():
    result = build("# Title\nText.")
    for obj in (result.context_units[0], result.context_links[0]):
        with pytest.raises(ValidationError):
            type(obj).model_validate({**obj.model_dump(), "extra": True})
        field = next(iter(type(obj).model_fields))
        with pytest.raises(ValidationError):
            setattr(obj, field, "changed")
    for updates in ({"page_start": 2}, {"page_start": 3, "page_end": 2}, {"source_uri": "file://x"}):
        with pytest.raises(ValidationError):
            KnowledgeContextUnit.model_validate({**result.context_units[0].model_dump(), **updates})
    for ordinal in (-1, 0.5, True):
        with pytest.raises(ValidationError):
            KnowledgeContextLink(chunk_id="c", context_id="p", child_ordinal=ordinal)


@pytest.mark.parametrize("kind", ["model_development", "incident"])
def test_real_fixtures_and_baseline_regression(kind):
    path = Path(__file__).resolve().parents[3] / "docs" / "runbooks" / f"credit_v3_{kind}_report_fixture.md"
    doc = document(path.read_text(encoding="utf-8"))
    baseline = BaselineIndexBuildStrategy().build(doc)
    assert baseline.search_chunks == tuple(chunking.chunk_document(doc))
    assert baseline.context_units == baseline.context_links == ()
    result = ParentChildIndexBuildStrategy().build(doc)
    assert result == ParentChildIndexBuildStrategy().build(doc)
    assert result.search_chunks and result.context_units
    assert not ({c.chunk_id for c in baseline.search_chunks} & {c.chunk_id for c in result.search_chunks})


def test_empty_searchable_document_and_unclosed_fence():
    assert build("# Title").model_dump() == {
        "search_chunks": (), "context_units": (), "context_links": (),
    }
    with pytest.raises(ValueError, match="unclosed fenced"):
        build("# Title\n```text\nUnclosed")
