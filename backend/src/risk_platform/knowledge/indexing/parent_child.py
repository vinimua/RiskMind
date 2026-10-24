"""Pure parent-first preparation; no storage, embedding or retrieval calls.

Reuse the frozen chunker's parsing primitives so Markdown interpretation remains
shared. Parent offsets retain source characters/pages before any child overlap.
"""

import hashlib
import json

from risk_platform.knowledge import chunking as parsing
from risk_platform.knowledge.chunking import KnowledgeChunk, KnowledgeDocument
from risk_platform.knowledge.indexing.contracts import (
    KnowledgeContextLink, KnowledgeContextUnit, PreparedKnowledge,
)


PARENT_CHILD_SCHEME_VERSION = "v1"
PARENT_TARGET_SIZE = 1200
PARENT_MAX_SIZE = 1600
PARENT_OVERLAP = 0
CHILD_SIZE = 400
CHILD_OVERLAP = 60


def _identity(kind: str, *parts: object) -> str:
    payload = json.dumps(
        ["parent-child", PARENT_CHILD_SCHEME_VERSION, kind, *parts],
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"pc-{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _parent_ranges(section: parsing._Section) -> list[tuple[int, int]]:
    """Keep short sections intact; pack long prose/atomic ranges in source order."""
    annotated = parsing._join_lines(section.lines)
    whole = parsing._trim_range(annotated.text, 0, len(annotated.text))
    if whole is None:
        return []
    blocks = parsing._split_blocks(section.lines)
    # Locate blocks by line identity, including blank separators between blocks.
    offsets = {}
    cursor = 0
    for line in section.lines:
        offsets[id(line)] = cursor
        cursor += len(line.text) + 1
    units = []
    for block in blocks:
        start = offsets[id(block.lines[0])]
        end = offsets[id(block.lines[-1])] + len(block.lines[-1].text)
        trimmed = parsing._trim_range(annotated.text, start, end)
        if trimmed is None:
            continue
        start, end = trimmed
        if block.kind != "prose":
            if end - start > PARENT_MAX_SIZE:
                raise ValueError(f"atomic {block.kind} block exceeds parent hard max")
            units.append((start, end))
        else:
            units.extend(parsing._recursive_ranges(
                annotated.text, start, end, PARENT_TARGET_SIZE, 0,
            ))
    if whole[1] - whole[0] <= PARENT_MAX_SIZE:
        return [whole]
    result: list[tuple[int, int]] = []
    for start, end in units:
        if result and end - result[-1][0] <= PARENT_TARGET_SIZE:
            result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
    return result


def _parent_lines(
    section: parsing._Section, start: int, end: int,
) -> tuple[parsing._SourceLine, ...]:
    # Slice original lines, retaining the parser's fence flags and exact pages.
    # Parent boundaries never fall inside atomic blocks.
    lines = []
    cursor = 0
    for line in section.lines:
        line_end = cursor + len(line.text)
        if cursor < end and line_end >= start:
            lines.append(parsing._SourceLine(
                text=line.text[max(0, start - cursor):min(len(line.text), end - cursor)],
                page_number=line.page_number, in_fence=line.in_fence,
            ))
        cursor = line_end + 1
    return tuple(lines)


class ParentChildIndexBuildStrategy:
    name = "parent-child-v1"

    def build(self, document: KnowledgeDocument) -> PreparedKnowledge:
        parents = []
        # Complete parent construction before generating any children.
        sources = []
        metadata = document.model_dump(exclude={"pages"})
        for section in parsing._split_sections(document):
            annotated = parsing._join_lines(section.lines)
            for start, end in _parent_ranges(section):
                content = annotated.text[start:end]
                section_path = " / ".join(section.path)
                page_start, page_end = parsing._page_span(annotated.pages, [(start, end)])
                parent = KnowledgeContextUnit(
                    **metadata, section_path=section_path, content=content,
                    page_start=page_start, page_end=page_end,
                    context_id=_identity(
                        "parent", document.document_id, document.document_version,
                        section_path, len(parents), content,
                    ),
                )
                parents.append(parent)
                sources.append(_parent_lines(section, start, end))

        chunks = []
        links = []
        for parent, lines in zip(parents, sources, strict=True):
            pieces = []
            for block in parsing._split_blocks(lines):
                if block.kind == "code":
                    pieces.append(parsing._atomic_code_piece(block.lines, CHILD_SIZE))
                elif block.kind == "table":
                    pieces.extend(parsing._split_table(
                        block.lines, parsing.DEFAULT_TABLE_MAX_ROWS, CHILD_SIZE,
                    ))
                else:
                    pieces.extend(parsing._split_prose(block.lines, CHILD_SIZE, CHILD_OVERLAP))
            for ordinal, piece in enumerate(pieces):
                chunk_id = _identity("child", parent.context_id, ordinal, piece.content)
                chunks.append(KnowledgeChunk(
                    **metadata, chunk_id=chunk_id, section_path=parent.section_path,
                    content=piece.content, page_start=piece.page_start, page_end=piece.page_end,
                ))
                links.append(KnowledgeContextLink(
                    chunk_id=chunk_id, context_id=parent.context_id, child_ordinal=ordinal,
                ))
        return PreparedKnowledge(
            search_chunks=tuple(chunks), context_units=tuple(parents), context_links=tuple(links),
        )
