"""Parent-Child 纯构建策略：先从文档生成 Parent，再在每个 Parent 内切出 Child。

复用 Baseline 的底层章节、内容块和正文切分函数，但不调用 chunk_document()。
Parent 保存原文及来源页码，不添加 overlap；Child 的 overlap 不会反向污染 Parent。
本模块不访问数据库、Embedding 或检索服务。
"""

import hashlib
import json

from risk_platform.knowledge import chunking as parsing
from risk_platform.knowledge.chunking import KnowledgeChunk, KnowledgeDocument
from risk_platform.knowledge.indexing.contracts import (
    KnowledgeContextLink, KnowledgeContextUnit, PreparedKnowledge,
)


# 长度均按 Python 字符数计算；方案版本参与身份计算，调整切分规则需评估版本升级。
PARENT_CHILD_SCHEME_VERSION = "v1"
PARENT_TARGET_SIZE = 1200  # 长章节的正文切分及单元合并目标，不是所有 Parent 的绝对上限。
PARENT_MAX_SIZE = 1600  # 短章节整体保留、完整表格或代码块所允许的硬上限。
PARENT_OVERLAP = 0  # 声明 Parent 不重叠；实现通过不扩展原文区间保证，而非读取此常量。
CHILD_SIZE = 400  # Child 成品上限；表格和代码也必须满足。
CHILD_OVERLAP = 60  # 正文主体预留该空间，实际尾部句段可能更短或没有。

#统一生成Parent和Child的身份ID，避免重复
def _identity(kind: str, *parts: object) -> str:
    """用方案、版本、对象类型及身份字段生成确定性 ID，与 Baseline 的 ID 隔离。"""

    # JSON 数组保留字段边界和顺序；固定编码形式，避免运行时间或随机数影响结果。
    payload = json.dumps(
        ["parent-child", PARENT_CHILD_SCHEME_VERSION, kind, *parts],
        ensure_ascii=False, separators=(",", ":"),
    )
    return f"pc-{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _parent_ranges(section: parsing._Section) -> list[tuple[int, int]]:
    """返回章节原文中的 Parent 区间 [start, end)，不跨章节，也不添加 overlap。
    区间里是字符下标，原文中每个字符对应的页码在 section.lines 中可查。

    不超过 1600 字符的有效章节整体保留；更长章节按 1200 目标切正文，
    再按原文顺序合并放得下的单元。表格和代码作为完整单元，不从中间切开。
    """

    annotated = parsing._join_lines(section.lines)
#   annotated.text   # 章节完整正文
# annotated.pages  # 每个字符对应的页码  
    whole = parsing._trim_range(annotated.text, 0, len(annotated.text))
    if whole is None:
        return []
    blocks = parsing._split_blocks(section.lines)
    # 用原文行对象的身份定位绝对偏移，避免相同文字多次出现时查错位置。
    # 每行额外计算一个换行位置，与 _join_lines() 的拼接方式对应。
#     不同类型的 Parent 切分规则不同：
# - 正文允许递归切。
# - 表格和代码在 Parent 层作为整体单元保留。
    offsets = {}
    cursor = 0

# offsets：记录每一行在章节拼接文本中的起点。
# cursor：下一行开始的位置。
    for line in section.lines:
        offsets[id(line)] = cursor
        cursor += len(line.text) + 1
# 甲 甲 \n 乙 乙 乙
# 0  1  2  3  4  5
    units = []
    for block in blocks:
        # 单元范围从第一行起点延伸到最后一行末尾，不重建或拼接块内正文。
        start = offsets[id(block.lines[0])]
        end = offsets[id(block.lines[-1])] + len(block.lines[-1].text)
        trimmed = parsing._trim_range(annotated.text, start, end)
        if trimmed is None:
            continue
        start, end = trimmed
        if block.kind != "prose":
            # 在短章节整体返回之前，也必须先检查原子块是否超过硬上限。说明当前块是表格或代码。
            if end - start > PARENT_MAX_SIZE:
                raise ValueError(f"atomic {block.kind} block exceeds parent hard max")
            units.append((start, end))
        else:
            # 这里只切 Parent 正文区间，不添加 Child 的 60 字符 overlap。
            units.extend(parsing._recursive_ranges(
                annotated.text, start, end, PARENT_TARGET_SIZE, 0,
            ))
            
    if whole[1] - whole[0] <= PARENT_MAX_SIZE:
        # 1201～1600 字符的章节也可整体保留，1200 是目标而非绝对上限。
        return [whole]
    result: list[tuple[int, int]] = []
    for start, end in units:
        if result and end - result[-1][0] <= PARENT_TARGET_SIZE:
            # 按原文整体跨度判断能否合并，块间空白也计入长度并保留。
            result[-1] = (result[-1][0], end)
        else:
            # 单个原子块可以超过 1200，但前面已保证不超过 1600。
            result.append((start, end))
    return result


def _parent_lines(
    section: parsing._Section, start: int, end: int,
) -> tuple[parsing._SourceLine, ...]:
    """把 Parent 字符区间映射回原文行，保留每行页码和代码围栏标记。

    正文边界可能落在一行中间，因此需换算为行内切片下标；
    表格和代码的 Parent 边界不会落在原子块内部。
    """

    lines = []
    cursor = 0
    for line in section.lines:
        line_end = cursor + len(line.text)
        if cursor < end and line_end >= start:
            # 将章节内绝对偏移换成行内偏移，并夹在当前行合法范围内。
            # 行尾等于 start 时可能保留空行，用于延续后续行的拼接结构。
            lines.append(parsing._SourceLine(
                text=line.text[max(0, start - cursor):min(len(line.text), end - cursor)],
                page_number=line.page_number, in_fence=line.in_fence,
            ))
        cursor = line_end + 1
    return tuple(lines)


class ParentChildIndexBuildStrategy:
    """产出搜索 Child、阅读 Parent，以及每个 Child 唯一的 Parent 关联。"""

    name = "parent-child-v1"

    def build(self, document: KnowledgeDocument) -> PreparedKnowledge:
        """完成全部 Parent 后再构建 Child，返回可供后续持久化的统一准备结果。"""

        parents = []
        # 第一阶段：完成所有 Parent，sources 与 parents 按顺序一一对应。
        sources = []
        # 文档级元数据可复用；排除 pages，避免把整份页面列表复制到每个结果。
        metadata = document.model_dump(exclude={"pages"})
        for section in parsing._split_sections(document):
            annotated = parsing._join_lines(section.lines)
            for start, end in _parent_ranges(section):
                # Parent 内容直接取原文切片，不从 Child 反向拼接。
                content = annotated.text[start:end]
                section_path = " / ".join(section.path)
                page_start, page_end = parsing._page_span(annotated.pages, [(start, end)])
                parent = KnowledgeContextUnit(
                    **metadata, section_path=section_path, content=content,
                    page_start=page_start, page_end=page_end,
                    context_id=_identity(
                        # Parent 身份包含文档、版本、章节、全局 Parent 序号和原文。
                        "parent", document.document_id, document.document_version,
                        section_path, len(parents), content,
                    ),
                )
                parents.append(parent)
                sources.append(_parent_lines(section, start, end))

        # 第二阶段：只在各自 Parent 范围内生成 Child，不跨 Parent 添加 overlap。
        chunks = []
        links = []
        # strict=True 确保 Parent 与来源行数量相等，防止静默遗漏。
        for parent, lines in zip(parents, sources, strict=True):
            pieces = []
            for block in parsing._split_blocks(lines):
                if block.kind == "code":
                    # Parent 能容纳代码不代表 Child 一定能容纳：超过 400 仍会失败。
                    pieces.append(parsing._atomic_code_piece(block.lines, CHILD_SIZE))
                elif block.kind == "table":
                    # 按完整数据行切 Child，每片重复表头，并限制行数及 400 字符长度。
                    pieces.extend(parsing._split_table(
                        block.lines, parsing.DEFAULT_TABLE_MAX_ROWS, CHILD_SIZE,
                    ))
                else:
                    # 正文复用 400/60 规则：主体最多 340，再补入最多 60 的尾部句段。
                    pieces.extend(parsing._split_prose(block.lines, CHILD_SIZE, CHILD_OVERLAP))
            for ordinal, piece in enumerate(pieces):
                # ordinal 从每个 Parent 内的 0 开始；Child ID 绑定 Parent，不能跨父复用。
                chunk_id = _identity("child", parent.context_id, ordinal, piece.content)
                chunks.append(KnowledgeChunk(
                    **metadata, chunk_id=chunk_id, section_path=parent.section_path,
                    content=piece.content, page_start=piece.page_start, page_end=piece.page_end,
                ))
                links.append(KnowledgeContextLink(
                    # 显式记录归属和父内顺序，后续解析无需猜测 Child 属于哪个 Parent。
                    chunk_id=chunk_id, context_id=parent.context_id, child_ordinal=ordinal,
                ))
        return PreparedKnowledge(
            # search_chunks 用于搜索，context_units 用于阅读，context_links 负责连接两者。
            search_chunks=tuple(chunks), context_units=tuple(parents), context_links=tuple(links),
        )
