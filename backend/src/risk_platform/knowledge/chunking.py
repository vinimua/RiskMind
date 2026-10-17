"""Deterministic, section-aware chunking for RiskMind knowledge documents."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.contracts import KnowledgeDocumentType


DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 60
DEFAULT_TABLE_MAX_ROWS = 10
CHUNKING_SCHEME_VERSION = "v1"

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_HEADING_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*[.)、]?[ \t]+")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+|[^。！？!?…\n]+$")
_PROSE_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")
_STABLE_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_EXCLUDED_SECTION_TITLES = frozenset({"修订历史", "目录", "文档说明"})


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


_NonBlankString = Annotated[str, AfterValidator(_validate_non_blank)]


def _validate_source_uri(value: str) -> str:
    value = _validate_non_blank(value)
    if not _STABLE_URI_RE.fullmatch(value) or value.lower().startswith("file://"):
        raise ValueError("source_uri must be a stable non-file URI")
    return value


_StableSourceUri = Annotated[str, AfterValidator(_validate_source_uri)]


class DocumentPage(BaseModel):
    """One parser-produced page; Markdown sources normally use an unnumbered page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: _NonBlankString
    page_number: int | None = Field(default=None, gt=0)


class KnowledgeDocument(BaseModel):
    """Stable document metadata plus Markdown content emitted by a parser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: _NonBlankString
    document_version: _NonBlankString
    document_type: KnowledgeDocumentType
    title: _NonBlankString
    source_uri: _StableSourceUri
    model_id: _NonBlankString | None = None
    pages: tuple[DocumentPage, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_page_numbers(self) -> Self:
        page_numbers = [page.page_number for page in self.pages]
        numbered = [number for number in page_numbers if number is not None]
        if numbered and len(numbered) != len(page_numbers):
            raise ValueError("document pages must be either all numbered or all unnumbered")
        if numbered != sorted(set(numbered)):
            raise ValueError("document page numbers must be unique and increasing")
        return self


class KnowledgeChunk(BaseModel):
    """A deterministic chunk ready for later persistence and vectorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: _NonBlankString
    document_id: _NonBlankString
    document_type: KnowledgeDocumentType
    document_version: _NonBlankString
    title: _NonBlankString
    section_path: str
    content: _NonBlankString
    model_id: _NonBlankString | None = None
    source_uri: _StableSourceUri
    page_start: int | None = Field(default=None, gt=0)
    page_end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_page_span(self) -> Self:
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("chunk page range requires both page_start and page_end")
        if self.page_start is not None and self.page_start > self.page_end:
            raise ValueError("chunk page_start cannot be greater than page_end")
        return self


@dataclass(frozen=True)
class _SourceLine:
    text: str
    page_number: int | None
    in_fence: bool = False


@dataclass(frozen=True)
class _Section:
    path: tuple[str, ...]
    lines: tuple[_SourceLine, ...]


@dataclass(frozen=True)
class _Block:
    kind: Literal["prose", "table", "code"]
    lines: tuple[_SourceLine, ...]


@dataclass(frozen=True)
class _AnnotatedText:
    text: str
    pages: tuple[int | None, ...]


@dataclass(frozen=True)
class _Piece:
    content: str
    page_start: int | None
    page_end: int | None


def chunk_document(document: KnowledgeDocument) -> list[KnowledgeChunk]:
    """Apply the frozen V1 chunking scheme without calling an LLM."""

    return _chunk_document_v1(
        document,
        chunk_size=DEFAULT_CHUNK_SIZE,
        overlap=DEFAULT_CHUNK_OVERLAP,
        table_max_rows=DEFAULT_TABLE_MAX_ROWS,
    )


def _chunk_document_v1(
    document: KnowledgeDocument,
    *,
    chunk_size: int,
    overlap: int,
    table_max_rows: int,
) -> list[KnowledgeChunk]:
    """Configurable V1 implementation used only by focused algorithm tests."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    if table_max_rows <= 0:
        raise ValueError("table_max_rows must be positive")

    pending: list[tuple[str, _Piece]] = []
    for section in _split_sections(document):
        section_path = " / ".join(section.path)
        for block in _split_blocks(section.lines):
            if block.kind == "table":
                pieces = _split_table(block.lines, table_max_rows, chunk_size)
            elif block.kind == "code":
                pieces = [_atomic_code_piece(block.lines, chunk_size)]
            else:
                pieces = _split_prose(block.lines, chunk_size, overlap)
            pending.extend((section_path, piece) for piece in pieces)

    chunks: list[KnowledgeChunk] = []
    for index, (section_path, piece) in enumerate(pending):
        chunks.append(
            KnowledgeChunk(
                chunk_id=_stable_chunk_id(
                    document=document,
                    index=index,
                    section_path=section_path,
                    content=piece.content,
                ),
                document_id=document.document_id,
                document_type=document.document_type,
                document_version=document.document_version,
                title=document.title,
                section_path=section_path,
                content=piece.content,
                model_id=document.model_id,
                source_uri=document.source_uri,
                page_start=piece.page_start,
                page_end=piece.page_end,
            )
        )
    return chunks


def _split_sections(document: KnowledgeDocument) -> list[_Section]:
    sections: list[_Section] = []
    headings: dict[int, str] = {}
    current_path: tuple[str, ...] = ()
    current_lines: list[_SourceLine] = []
    fence: str | None = None
    document_heading_seen = False

    def flush() -> None:
        nonlocal current_lines
        if (
            _has_meaningful_content(current_lines)
            and not _is_excluded_section(current_path)
        ):
            sections.append(_Section(path=current_path, lines=tuple(current_lines)))
        current_lines = []

    for page in document.pages:
        page_content = _HTML_COMMENT_RE.sub("", page.content)
        for raw_line in page_content.splitlines():
            fence_match = _FENCE_RE.match(raw_line)
            if fence is None:
                heading_match = _HEADING_RE.match(raw_line)
                if heading_match is not None:
                    flush()
                    level = len(heading_match.group(1))
                    if level == 1 and not document_heading_seen:
                        document_heading_seen = True
                        headings = {}
                        current_path = ()
                        continue
                    headings = {key: value for key, value in headings.items() if key < level}
                    headings[level] = _normalize_heading(heading_match.group(2))
                    current_path = tuple(headings[key] for key in sorted(headings))
                    continue

                in_fence = fence_match is not None
                if fence_match is not None:
                    fence = fence_match.group(1)
            else:
                in_fence = True
                if fence_match is not None and fence_match.group(1)[0] == fence[0]:
                    fence = None

            current_lines.append(
                _SourceLine(
                    text=raw_line,
                    page_number=page.page_number,
                    in_fence=in_fence,
                )
            )
    if fence is not None:
        raise ValueError("document contains an unclosed fenced code block")
    flush()
    return sections


def _normalize_heading(title: str) -> str:
    return _HEADING_NUMBER_RE.sub("", title.strip())


def _is_excluded_section(path: tuple[str, ...]) -> bool:
    return any(part in _EXCLUDED_SECTION_TITLES for part in path)


def _has_meaningful_content(lines: list[_SourceLine]) -> bool:
    return any(
        line.text.strip() and not _HORIZONTAL_RULE_RE.match(line.text)
        for line in lines
    )


def _split_blocks(lines: tuple[_SourceLine, ...]) -> list[_Block]:
    blocks: list[_Block] = []
    prose: list[_SourceLine] = []
    index = 0

    def flush_prose() -> None:
        nonlocal prose
        if _has_meaningful_content(prose):
            blocks.append(_Block(kind="prose", lines=tuple(prose)))
        prose = []

    while index < len(lines):
        fence_match = _FENCE_RE.match(lines[index].text)
        if fence_match is not None and lines[index].in_fence:
            flush_prose()
            delimiter = fence_match.group(1)[0]
            code = [lines[index]]
            index += 1
            while index < len(lines):
                code.append(lines[index])
                closing = _FENCE_RE.match(lines[index].text)
                index += 1
                if closing is not None and closing.group(1)[0] == delimiter:
                    break
            blocks.append(_Block(kind="code", lines=tuple(code)))
            continue
        if _starts_table(lines, index):
            flush_prose()
            table = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines):
                line = lines[index]
                if line.in_fence or not line.text.strip() or "|" not in line.text:
                    break
                table.append(line)
                index += 1
            blocks.append(_Block(kind="table", lines=tuple(table)))
            continue
        prose.append(lines[index])
        index += 1

    flush_prose()
    return blocks


def _starts_table(lines: tuple[_SourceLine, ...], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header, separator = lines[index], lines[index + 1]
    return (
        not header.in_fence
        and not separator.in_fence
        and "|" in header.text
        and _is_table_separator(separator.text)
    )


def _is_table_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return bool(cells) and all(
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) is not None for cell in cells
    )


def _split_table(
    lines: tuple[_SourceLine, ...], max_rows: int, chunk_size: int
) -> list[_Piece]:
    header = lines[:2]
    rows = lines[2:]
    if len(_piece_from_lines(header).content) > chunk_size:
        raise ValueError("table header exceeds chunk_size")
    if not rows:
        return [_piece_from_lines(header)]

    pieces: list[_Piece] = []
    group: list[_SourceLine] = []
    for row in rows:
        one_row = _piece_from_lines((*header, row))
        if len(one_row.content) > chunk_size:
            raise ValueError("table header plus one row exceeds chunk_size")

        candidate = _piece_from_lines((*header, *group, row))
        if group and (len(group) >= max_rows or len(candidate.content) > chunk_size):
            pieces.append(_piece_from_lines((*header, *group)))
            group = [row]
        else:
            group.append(row)
    if group:
        pieces.append(_piece_from_lines((*header, *group)))
    return pieces


def _atomic_code_piece(
    lines: tuple[_SourceLine, ...], chunk_size: int
) -> _Piece:
    piece = _piece_from_lines(lines)
    if len(piece.content) > chunk_size:
        raise ValueError("fenced code block exceeds chunk_size")
    return piece


def _split_prose(
    lines: tuple[_SourceLine, ...],
    chunk_size: int,
    overlap: int,
) -> list[_Piece]:
    annotated = _join_lines(lines)
    body_size = chunk_size - overlap
    ranges = _recursive_ranges(annotated.text, 0, len(annotated.text), body_size, 0)
    pieces: list[_Piece] = []
    for index, current in enumerate(ranges):
        source_ranges = [current]
        content_parts = []
        if index > 0 and overlap:
            previous = ranges[index - 1]
            overlap_range = _trailing_sentence_range(
                annotated.text,
                previous[0],
                previous[1],
                overlap,
            )
            if overlap_range is not None:
                source_ranges.append(overlap_range)
                content_parts.append(annotated.text[slice(*overlap_range)])
        content_parts.append(annotated.text[slice(*current)])
        content = "".join(content_parts).strip()
        if content and not _HORIZONTAL_RULE_RE.fullmatch(content):
            if len(content) > chunk_size:
                raise AssertionError("prose chunk exceeds the configured chunk_size")
            page_start, page_end = _page_span(annotated.pages, source_ranges)
            pieces.append(
                _Piece(content=content, page_start=page_start, page_end=page_end)
            )
    return pieces


def _recursive_ranges(
    text: str,
    start: int,
    end: int,
    chunk_size: int,
    separator_index: int,
) -> list[tuple[int, int]]:
    trimmed = _trim_range(text, start, end)
    if trimmed is None:
        return []
    start, end = trimmed
    if end - start <= chunk_size:
        return [(start, end)]
    if separator_index >= len(_PROSE_SEPARATORS):
        return [
            trimmed_range
            for offset in range(start, end, chunk_size)
            if (trimmed_range := _trim_range(text, offset, min(offset + chunk_size, end)))
            is not None
        ]

    separator = _PROSE_SEPARATORS[separator_index]
    segments = _ranges_split_on(text, start, end, separator)
    if len(segments) == 1:
        return _recursive_ranges(
            text, start, end, chunk_size, separator_index + 1
        )

    result: list[tuple[int, int]] = []
    pending: tuple[int, int] | None = None
    for segment in segments:
        if segment[1] - segment[0] > chunk_size:
            if pending is not None:
                result.append(pending)
                pending = None
            result.extend(
                _recursive_ranges(
                    text,
                    segment[0],
                    segment[1],
                    chunk_size,
                    separator_index + 1,
                )
            )
            continue

        if pending is None:
            pending = segment
        elif segment[1] - pending[0] <= chunk_size:
            pending = (pending[0], segment[1])
        else:
            result.append(pending)
            pending = segment
    if pending is not None:
        result.append(pending)
    return result


def _ranges_split_on(
    text: str, start: int, end: int, separator: str
) -> list[tuple[int, int]]:
    ranges = []
    cursor = start
    while cursor < end:
        position = text.find(separator, cursor, end)
        segment_end = end if position == -1 else position + len(separator)
        trimmed = _trim_range(text, cursor, segment_end)
        if trimmed is not None:
            ranges.append(trimmed)
        if position == -1:
            break
        cursor = segment_end
    return ranges


def _trim_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return None if start == end else (start, end)


def _trailing_sentence_range(
    text: str,
    start: int,
    end: int,
    max_chars: int,
) -> tuple[int, int] | None:
    matches = [match for match in _SENTENCE_RE.finditer(text, start, end)]
    selected: list[re.Match[str]] = []
    total = 0
    for match in reversed(matches):
        if not match.group().strip():
            continue
        sentence_length = len(match.group())
        if sentence_length > max_chars or total + sentence_length > max_chars:
            break
        selected.insert(0, match)
        total += sentence_length
    if not selected:
        return None
    return _trim_range(text, selected[0].start(), selected[-1].end())


def _join_lines(lines: tuple[_SourceLine, ...]) -> _AnnotatedText:
    text_parts: list[str] = []
    pages: list[int | None] = []
    for index, line in enumerate(lines):
        if index:
            text_parts.append("\n")
            pages.append(line.page_number)
        text_parts.append(line.text)
        pages.extend([line.page_number] * len(line.text))
    return _AnnotatedText(text="".join(text_parts), pages=tuple(pages))


def _piece_from_lines(lines: tuple[_SourceLine, ...]) -> _Piece:
    content = "\n".join(line.text for line in lines).strip()
    numbers = [line.page_number for line in lines if line.page_number is not None]
    return _Piece(
        content=content,
        page_start=min(numbers) if numbers else None,
        page_end=max(numbers) if numbers else None,
    )


def _page_span(
    pages: tuple[int | None, ...], ranges: list[tuple[int, int]]
) -> tuple[int | None, int | None]:
    numbers = {
        page
        for start, end in ranges
        for page in pages[start:end]
        if page is not None
    }
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def _stable_chunk_id(
    *,
    document: KnowledgeDocument,
    index: int,
    section_path: str,
    content: str,
) -> str:
    payload = "\x1f".join(
        (
            CHUNKING_SCHEME_VERSION,
            document.document_id,
            document.document_version,
            str(index),
            section_path,
            content,
        )
    )
    return f"chunk-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
