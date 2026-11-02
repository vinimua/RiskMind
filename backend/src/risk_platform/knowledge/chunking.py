"""知识文档的 Baseline 分块算法：按章节和内容类型切分，并保留来源页码。

处理顺序:
KnowledgeDocument
    ↓ _split_sections()
章节 Section
    ↓ _split_blocks()
正文 / 表格 / 代码块
    ↓ 各自切分
中间片段 Piece
    ↓ 补齐来源、生成 ID
KnowledgeChunk[]
只执行确定性文本处理，不调用 LLM、Embedding 或数据库。
"""

from __future__ import annotations  # 延迟求值类型标注，便于声明函数和数据结构。

import hashlib  # 为片段内容和来源身份计算稳定哈希。
import re  # 识别标题、代码围栏、表格分隔行和句段。
from dataclasses import dataclass  # 为内部数据容器生成初始化等方法。
from typing import Annotated, Literal, Self  # 附加校验元数据、限定字面值、标注自身类型。

#Annotated	给字符串类型附加 Pydantic 校验规则
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator  # 定义并校验输入输出。

from risk_platform.knowledge.contracts import KnowledgeDocumentType  # 复用知识文档分类枚举。


# 长度按 Python 字符数计算，不是 token 数；正文为 overlap 预留空间。
DEFAULT_CHUNK_SIZE = 400  # 最终片段正文的最大字符数。
DEFAULT_CHUNK_OVERLAP = 60  # 正文可从前一主体补入的最大句段长度。
DEFAULT_TABLE_MAX_ROWS = 10  # 每个表格片段最多包含的数据行数，不含重复表头。
CHUNKING_SCHEME_VERSION = "v1" # 分块算法方案的版本，用于生成 Chunk ID

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")  # 捕获标题层级和标题文字。
#识别标题前面的数字编号
_HEADING_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)*[.)、]?[ \t]+")  # 匹配 3、3.2 等前导编号。

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")  # 匹配反引号或波浪号代码围栏。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)  # 非贪婪匹配，可跨行删除 HTML 注释。
_HORIZONTAL_RULE_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")  # 识别纯分隔线。

_SENTENCE_RE = re.compile(r"[^。！？!?…\n]*[。！？!?…\n]+|[^。！？!?…\n]+$")  # 为 overlap 识别句段及无标点尾段。
# 正文递归切分时，依次尝试段落、换行、句子和更细的分隔符。
_PROSE_SEPARATORS = ("\n\n", "\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")  # 顺序就是递归切分的优先级。
_STABLE_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")  # 来源必须带协议，且地址部分不能含空白。
# 这些章节及其子章节不进入知识片段。
_EXCLUDED_SECTION_TITLES = frozenset({"修订历史", "目录", "文档说明"})  # 固定排除集合，不在运行中修改。


def _validate_non_blank(value: str) -> str:
    """校验字符串不为空或纯空白；成功时返回原值，保留两端空白。"""

    if not value.strip():  # strip 后为空，说明原值没有有效字符。
        raise ValueError("字段值不能为空或仅包含空白字符")  # 阻止无效输入进入后续流程。
    return value  # 保留原始字符串，不在校验时改写内容。


_NonBlankString = Annotated[str, AfterValidator(_validate_non_blank)]  # 字符串校验完成后再检查非空白。


def _validate_source_uri(value: str) -> str:
    """校验来源为非空且带协议的非 file:// URI；只检查格式，不访问地址。"""

    value = _validate_non_blank(value)  # 先复用非空白校验。
    if not _STABLE_URI_RE.fullmatch(value) or value.lower().startswith("file://"):  # 检查格式并排除本地文件协议。
        raise ValueError("source_uri 必须是稳定的来源 URI，不能使用 file:// 本地文件地址")  # 明确拒绝不符合来源约定的地址。
    return value  # 返回通过格式检查的原始地址，不访问网络。


_StableSourceUri = Annotated[str, AfterValidator(_validate_source_uri)]  # 将来源校验绑定到字符串字段。


class DocumentPage(BaseModel):
    """解析器提供的一页文本；Markdown 通常使用一个不带页码的页面。"""

    model_config = ConfigDict(extra="forbid", frozen=True)  # 拒绝未知字段，并禁止实例字段重新赋值。

    content: _NonBlankString  # 解析器提供的本页 Markdown 文本。
    page_number: int | None = Field(default=None, gt=0)  # 页码可缺省；提供时必须为正数。


class KnowledgeDocument(BaseModel):
    """分块输入：稳定的文档元数据，以及解析器输出的 Markdown 页面。

    frozen 禁止实例字段重新赋值，extra="forbid" 拒绝未定义字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)  # 保持文档结构明确，创建后不重新赋值字段。

    document_id: _NonBlankString  # 文档的稳定业务标识。
    document_version: _NonBlankString  # 本次内容对应的文档版本。
    document_type: KnowledgeDocumentType  # 开发报告或异常调查报告。
    title: _NonBlankString  # 文档标题，将复制到每个输出片段。
    source_uri: _StableSourceUri  # 可用于追溯来源的非本机文件 URI。
    model_id: _NonBlankString | None = None  # 报告关联的风控模型；通用知识可不提供。
    pages: tuple[DocumentPage, ...] = Field(min_length=1)  # 按原文顺序保存页面，至少一页。

    @model_validator(mode="after")  # 在所有字段解析完成后检查页码之间的关系。
    def validate_page_numbers(self) -> Self:
        """校验整份文档的页码一致性，成功后返回当前文档。

        允许全部无页码；有页码时必须全部提供、唯一且递增，不要求连续。
        """

        # 页码要么全部缺省，要么全部提供且严格递增，不允许混用或重复。
        page_numbers = [page.page_number for page in self.pages]  # 收集全部页码，包括 None。
        numbered = [number for number in page_numbers if number is not None]  # 取出明确提供的页码。
        if numbered and len(numbered) != len(page_numbers):  # 有编号页数少于总页数，说明混用了两种形式。
            raise ValueError("文档页面必须全部提供页码或全部不提供页码")  # 拒绝部分页面缺失页码。
        if numbered != sorted(set(numbered)):  # 去重排序后的序列不同，说明有重复或逆序。
            raise ValueError("文档页码必须唯一且严格递增")  # 不要求连续，但必须有序且不重复。
        return self  # 将校验后的文档交还给 Pydantic。


class KnowledgeChunk(BaseModel):
    """分块输出：正文片段、稳定 ID 和来源信息，供后续持久化与向量化使用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)  # 片段字段明确，创建后禁止重新赋值。

    chunk_id: _NonBlankString  # 当前片段的确定性身份标识。

    document_id: _NonBlankString  # 片段所属文档的 ID。
    document_type: KnowledgeDocumentType  # 继承文档类型，供后续筛选。
    document_version: _NonBlankString  # 明确片段来自文档的哪个版本。
    title: _NonBlankString  # 继承文档标题，便于展示来源。
    section_path: str  # 所属章节路径；无标题正文允许为空。
    content: _NonBlankString  # 实际分块正文，可能含 overlap、表头或代码围栏。
    model_id: _NonBlankString | None = None  # 继承文档关联的风控模型标识。
    source_uri: _StableSourceUri  # 继承原文档来源地址。
    page_start: int | None = Field(default=None, gt=0)  # 内容及 overlap 涉及的最小页码。
    page_end: int | None = Field(default=None, gt=0)  # 内容及 overlap 涉及的最大页码。

    @model_validator(mode="after")  # 字段解析完成后检查起止页是否构成有效范围。
    def validate_page_span(self) -> Self:
        """校验片段起止页同时存在或同时缺省，且起始页不大于结束页。"""

        # 未编号文档的起止页均为 None；编号文档必须提供合法的完整范围。
        if (self.page_start is None) != (self.page_end is None):  # 两端只有一端为 None 时，布尔值不相等。
            raise ValueError("片段页码范围必须同时提供 page_start 和 page_end")  # 拒绝不完整的页码范围。
        if self.page_start is not None and self.page_start > self.page_end:  # 有页码时进一步检查先后顺序。
            raise ValueError("片段起始页 page_start 不能大于结束页 page_end")  # 拒绝倒置范围。
        return self  # 返回通过范围检查的片段。

#dataclass 会自动生成初始化等常用方法。
@dataclass(frozen=True)  # 自动生成构造方法，并禁止字段重新赋值。
class _SourceLine:
    """内部结构：原文行及其 页码；标记代码围栏内的行，避免将代码中的 # 误识别为标题。"""

    text: str  # 该行原文，不包含 splitlines 已移除的行结束符。
    page_number: int | None  # 此行所在页；无编号来源使用 None。
    in_fence: bool = False  # 是否属于代码围栏区域，包括围栏行本身。


@dataclass(frozen=True)  # 将章节路径和原文行保存在不可重新赋值的容器中。
class _Section:
    """章节标题路径与该章节的原文行；片段不会跨章节合并。"""

    path: tuple[str, ...]  # 从上级到下级的规范化章节标题。
    lines: tuple[_SourceLine, ...]  # 此章节按原文顺序保存的正文行。


@dataclass(frozen=True)  # 保存识别结果，避免后续切分意外改变内容块身份。
class _Block:
    """章节内的内容块，不同类型使用不同的切分规则。"""

    kind: Literal["prose", "table", "code"]  # 只允许正文、表格、代码三种处理类型。
    lines: tuple[_SourceLine, ...]  # 当前内容块的完整原文行。


@dataclass(frozen=True)  # 文本和页码映射创建后保持对应关系。
class _AnnotatedText:
    """拼接文本与逐字符页码映射，满足 pages[i] 对应 text[i]。"""

    text: str  # 用换行连接后的完整正文字符串。
    pages: tuple[int | None, ...]  # 每个字符的来源页码，与 text 等长。


@dataclass(frozen=True)  # 中间片段创建后不通过重新赋值改写其内容或页码。
class _Piece:
    """尚未补齐文档元数据和 chunk_id 的中间片段。"""

    content: str  # 已经切好的文本，尚未补齐文档信息和片段 ID。
    page_start: int | None  # 片段所涉及的最小来源页码。
    page_end: int | None  # 片段所涉及的最大来源页码。


def chunk_document(document: KnowledgeDocument) -> list[KnowledgeChunk]:
    """公共入口：使用已封板的 V1 参数切分，相同输入产生相同内容、顺序和 ID。"""

    return _chunk_document_v1(  # 委托 V1 实现，并直接返回其片段列表。
        document,  # 传入整份文档对象，不是文件名或某个片段。
        chunk_size=DEFAULT_CHUNK_SIZE,  # 固定成品片段上限为 400 字符。
        overlap=DEFAULT_CHUNK_OVERLAP,  # 为正文重叠句段预留最多 60 字符。
        table_max_rows=DEFAULT_TABLE_MAX_ROWS,  # 表格每片最多 10 个数据行。
    )


def _chunk_document_v1(
    document: KnowledgeDocument,  # 已解析的完整文档，含元数据和页面。
    *,  # 后续参数必须按名称传入，避免整数参数位置混淆。
    chunk_size: int,  # 所有成品片段的字符上限。
    overlap: int,  # 正文重叠句段的最大字符预算。
    table_max_rows: int,  # 表格片段的数据行上限。
) -> list[KnowledgeChunk]:  # 返回补齐文档身份的片段列表。
    """V1 实现；可调参数用于算法测试，正常调用应使用 chunk_document()。"""

    if chunk_size <= 0:  # 总长度必须能容纳至少一个字符。
        raise ValueError("片段长度上限 chunk_size 必须大于 0")  # 拒绝非法长度参数。
    if overlap < 0 or overlap >= chunk_size:  # overlap 不能为负，也不能占满主体空间。
        raise ValueError("重叠长度 overlap 必须大于等于 0 且小于 chunk_size")  # 拒绝无效重叠预算。
    if table_max_rows <= 0:  # 表格每组必须能容纳至少一行数据。
        raise ValueError("表格数据行数上限 table_max_rows 必须大于 0")  # 拒绝非法行数参数。

    # 先保留原文顺序收集片段，再统一补齐来源并生成文档内的全局序号。
    pending: list[tuple[str, _Piece]] = []  # 每项保存章节路径及其切分出的中间片段。
    for section in _split_sections(document):  # 按文档顺序遍历已过滤的章节。
        section_path = " / ".join(section.path)  # 将多级标题转换为便于展示的路径。
        for block in _split_blocks(section.lines):  # 在章节内识别正文、表格和代码。
            if block.kind == "table":  # 表格使用完整行分组，不添加正文 overlap。
                pieces = _split_table(block.lines, table_max_rows, chunk_size)  # 重复表头并限制行数及长度。
            elif block.kind == "code":  # 代码块必须作为一个整体保留。
                pieces = [_atomic_code_piece(block.lines, chunk_size)]  # 包装为列表，统一后续处理形式。
            else:  # 其余内容按普通正文处理。
                pieces = _split_prose(block.lines, chunk_size, overlap)  # 递归切主体，再补入尾部句段。
            pending.extend((section_path, piece) for piece in pieces)  # 按当前块内顺序追加，不重排。

    chunks: list[KnowledgeChunk] = []  # 收集补齐文档元数据后的最终片段。
    for index, (section_path, piece) in enumerate(pending):  # 从 0 开始生成文档内全局序号。
        chunks.append(  # 每次添加一个独立的输出对象。
            KnowledgeChunk(  # Pydantic 同时校验输出字段及来源页码。
                chunk_id=_stable_chunk_id(  # 从文档身份、顺序和正文计算 ID。
                    document=document,  # 提供原文档 ID 和版本。
                    index=index,  # 参与 ID 计算的是原文序号，不是检索排名。
                    section_path=section_path,  # 将章节位置纳入片段身份。
                    content=piece.content,  # 将实际输出正文纳入片段身份。
                ),
                document_id=document.document_id,  # 继承来源文档 ID。
                document_type=document.document_type,  # 继承报告分类。
                document_version=document.document_version,  # 继承来源版本。
                title=document.title,  # 继承文档级标题。
                section_path=section_path,  # 记录当前片段所属章节。
                content=piece.content,  # 保存切分结果，不在此处再次改写。
                model_id=document.model_id,  # 继承关联的风控模型。
                source_uri=document.source_uri,  # 保留文档来源 URI。
                page_start=piece.page_start,  # 保留中间片段计算出的最小页码。
                page_end=piece.page_end,  # 保留中间片段计算出的最大页码。
            )
        )
    return chunks  # 返回保持原文顺序的完整 Chunk 列表。


def _split_sections(document: KnowledgeDocument) -> list[_Section]:
    """按 Markdown 标题建立章节路径，跳过注释和排除章节，保留跨页的章节状态。"""

    sections: list[_Section] = []  # 已经完成的有效章节。
    headings: dict[int, str] = {}  # 当前标题层级到标题文字的映射，例如 {2: '调查', 3: '标签'}。
    current_path: tuple[str, ...] = ()  # 尚未遇到章节标题时，路径为空。
    current_lines: list[_SourceLine] = []  # 当前章节尚未提交的原文行。
    fence: str | None = None  # None 表示不在代码围栏内，否则记录开头围栏。
    document_heading_seen = False  # 用于识别并跳过第一个一级标题。


    #当前章节处理完了，把它收进结果。
    def flush() -> None:
        """提交当前章节的有效正文并清空行缓存；空章节和排除章节不入结果。"""

        nonlocal current_lines  # 后续清空操作需要重新绑定外层变量。
        if (  # 仅同时符合有效内容和章节保留规则时提交。
            _has_meaningful_content(current_lines)  # 章节至少包含一行有效内容。
            and not _is_excluded_section(current_path)  # 当前标题及父级标题都不能是排除项。
        ):
            sections.append(_Section(path=current_path, lines=tuple(current_lines)))  # 固定当前路径和行序列。
        #处理后清空，准备下一个章节。
        current_lines = []  # 即使章节被过滤，也必须清空缓存。

    for page in document.pages:  # 按输入页面顺序扫描，章节状态不会因换页而清空。
        #删除这一页里的 HTML 注释
        page_content = _HTML_COMMENT_RE.sub("", page.content)  # 移除该页内匹配到的 HTML 注释。
        for raw_line in page_content.splitlines():  # 按真实换行拆行，不是按编辑器视觉折行。
            fence_match = _FENCE_RE.match(raw_line)  # 预先检查本行是否以代码围栏开头。
            if fence is None:  # 只有代码区外的行才参与标题识别。
                heading_match = _HEADING_RE.match(raw_line)  # 尝试提取 # 层级和标题文字。
                if heading_match is not None:  # 新标题意味着上一章节已经结束。
                    flush()  # 提交标题前收集的正文。
                    level = len(heading_match.group(1))  # # 的个数就是标题层级。
                    if level == 1 and not document_heading_seen:  # 第一个 H1 作为文档级标题处理。
                        # 第一个一级标题作为文档标题，不放入章节路径。
                        document_heading_seen = True  # 后续一级标题不再触发此特殊分支。
                        headings = {}  # 重置文档标题之前的标题状态。
                        current_path = ()  # 文档标题不成为章节路径的一部分。
                        continue  # 标题行不追加到正文行缓存。
                    # 新标题替换同级及更深层标题，保留上级标题，形成当前完整路径。
                    headings = {key: value for key, value in headings.items() if key < level}  # 删除旧的同级和下级标题。
                    headings[level] = _normalize_heading(heading_match.group(2))  # 去掉编号后记录当前标题。
                    current_path = tuple(headings[key] for key in sorted(headings))  # 按层级从浅到深形成路径。
                    continue  # 章节标题也不作为正文重复保存。

                # 围栏内的 # 和 | 都属于代码，不能按章节或表格解析。
                in_fence = fence_match is not None  # 开始围栏行本身也属于代码块。
                if fence_match is not None:  # 本行打开一个新的代码围栏。
                    fence = fence_match.group(1)  # 记录围栏字符，以便后面识别关闭位置。
            else:  # 当前已经位于代码围栏内部。
                in_fence = True  # 包括结束围栏在内的当前行都标记为代码。
                if fence_match is not None and fence_match.group(1)[0] == fence[0]:  # 按围栏字符种类判定关闭。
                    fence = None  # 下一行恢复为普通 Markdown 解析状态。

            current_lines.append(  # 保存非标题行，交给后续内容块识别。
                _SourceLine(  # 将原文和解析来源绑定在一起。
                    text=raw_line,  # 保存本行文本。
                    page_number=page.page_number,  # 绑定本页页码，可能为 None。
                    in_fence=in_fence,  # 保存本行是否属于代码块。
                )
            )
    if fence is not None:  # 扫描完所有页面后仍在代码区，说明缺少结束围栏。
        raise ValueError("文档包含未闭合的代码围栏")  # 明确失败，避免把后续内容误当代码。
    flush()  # 最后一个章节没有后续标题触发提交，需要补交一次。
    return sections  # 返回按原文排列的有效章节。


def _normalize_heading(title: str) -> str:
    """去除标题前的章节编号，例如将“3.2 根因分析”转成“根因分析”。"""
    return _HEADING_NUMBER_RE.sub("", title.strip())  # 先清理两端空白，再移除匹配的前导编号。


def _is_excluded_section(path: tuple[str, ...]) -> bool:
    """判断章节路径中是否含排除标题；命中任意父级时，其子章节也被排除。"""

    return any(part in _EXCLUDED_SECTION_TITLES for part in path)  # 任意一级命中排除集合就返回 True。

#检查是否至少有一行内容不为空且不是水平分隔线，避免生成空章节。
def _has_meaningful_content(lines: list[_SourceLine]) -> bool:
    """检查是否至少有一行非空且不是水平分隔线，不修改或过滤传入的行。"""

    return any(  # 只需找到一行有效内容，不要求每一行都有效。
        line.text.strip() and not _HORIZONTAL_RULE_RE.match(line.text)  # 该行既非空白，也不是纯分隔线。
        for line in lines  # 按顺序检查原文行，但不修改它们。
    )


def _split_blocks(lines: tuple[_SourceLine, ...]) -> list[_Block]:
    """保持行顺序，把章节拆成正文、Markdown 表格和围栏代码块。"""

    blocks: list[_Block] = []  # 保存依次识别出的内容块。
    prose: list[_SourceLine] = []  # 暂存尚未遇到表格或代码的连续正文行。
    index = 0  # 指向当前尚未处理的行。
# blocks：最终识别出的内容块。
# prose：暂存连续正文行。
# index：当前读到哪一行。

    def flush_prose() -> None:
        """将缓存的有效正文行提交为 prose 块，再清空缓存以处理后续内容。"""

        nonlocal prose  # 清空时需要重新绑定外层正文缓存。
        if _has_meaningful_content(prose):  # 纯空白和纯分隔线不单独构成正文块。
            blocks.append(_Block(kind="prose", lines=tuple(prose)))  # 固定行顺序并标记为正文。
        prose = []  # 无论是否提交，都为下一段正文准备空缓存。
# 把暂存正文写入结果，并清空。

# 遇到表格或代码时，需要先结束前面的正文。
    while index < len(lines):  # 直到章节内所有行都被消费。
        fence_match = _FENCE_RE.match(lines[index].text)  # 检查当前行是否有代码围栏。
        if fence_match is not None and lines[index].in_fence:  # 代码块优先于表格和正文识别。
            flush_prose()  # 保证前面的正文排在代码块前面。
            delimiter = fence_match.group(1)[0]  # 保存反引号或波浪号这一个字符。
            code = [lines[index]]  # 代码片段包含起始围栏行。
            index += 1  # 从围栏后的下一行继续扫描。
            while index < len(lines):  # 连续读取，直到同类结束围栏。
                code.append(lines[index])  # 当前行先放入代码块，结束围栏也会被保留。
                closing = _FENCE_RE.match(lines[index].text)  # 判断当前行是否为候选结束围栏。
                index += 1  # 无论是否关闭，当前行都已经处理完成。
                #接着继续读取，直到同类结束围栏
                if closing is not None and closing.group(1)[0] == delimiter:  # 围栏字符与开头一致。
                    break  # 结束当前代码块的收集。
            blocks.append(_Block(kind="code", lines=tuple(code)))  # 整块保留代码及围栏。
            continue  # 已消费的代码行不能再次进入正文或表格分支。
        if _starts_table(lines, index):  # 检查当前行及下一行是否构成表头。
            flush_prose()  # 先提交表格之前的正文。
#| 指标 | 数值 |
# |---|---|
            table = [lines[index], lines[index + 1]]  # 表头包括列名行和 Markdown 分隔行。
            index += 2  # 两行表头已处理，开始读取数据行。
            while index < len(lines):  # 连续收集符合当前表格行规则的后续行。
                line = lines[index]  # 读取当前候选数据行。
                if line.in_fence or not line.text.strip() or "|" not in line.text:  # 代码、空行或无竖线行结束表格。
                    break  # 不消费该行，交给外层循环重新识别。
                table.append(line)  # 将这一完整行加入表格。
                index += 1  # 移动到下一候选数据行。
            blocks.append(_Block(kind="table", lines=tuple(table)))  # 此时只识别表格，尚未按行数拆片。
            continue  # 表格已整体消费，继续识别后面的内容。
        prose.append(lines[index])  # 未识别为表格或代码的行进入正文缓存。
        index += 1  # 当前正文行已处理，继续向后移动。
#循环结束后再 flush_prose()，避免最后一段正文遗漏。
    flush_prose()  # 提交末尾没有被表格或代码触发提交的正文。
    return blocks  # 返回保持章节内原文顺序的内容块。


def _starts_table(lines: tuple[_SourceLine, ...], index: int) -> bool:
    """判断指定行与下一行是否构成非代码区的表头及分隔行；不足两行返回 False。"""

    # 同时检查表头和下一行的 Markdown 分隔行，不能只凭出现 | 判定表格。
    if index + 1 >= len(lines):  # 至少需要两行才能构成表头。
        return False  # 不足两行时不是表格起点。
    header, separator = lines[index], lines[index + 1]  # 分别取出列名行和分隔行。
    return (  # 下列条件必须同时满足。
        not header.in_fence  # 列名行不能属于代码。
        and not separator.in_fence  # 分隔行也不能属于代码。
        and "|" in header.text  # 列名行需要包含表格列分隔符。
        and _is_table_separator(separator.text)  # 下一行各列必须是合法的 Markdown 分隔格式。
    )


def _is_table_separator(line: str) -> bool:
    """判断一行是否为 Markdown 表格分隔行，如 |---|:---:|，各列至少三个短横线。每格必须有至少三个 -，两侧可以带 :，表示 Markdown 对齐方式。"""

    cells = line.strip().strip("|").split("|")  # 去掉外侧空白和竖线，再按列拆分。
    return bool(cells) and all(  # 每个分隔单元格都要通过格式检查。
        re.fullmatch(r"\s*:?-{3,}:?\s*", cell) is not None for cell in cells  # 至少三个短横线，两侧可带对齐冒号。
    )


def _split_table(
    lines: tuple[_SourceLine, ...], max_rows: int, chunk_size: int  # 表格原文行、数据行数上限、字符上限。
) -> list[_Piece]:  # 一张表可能切成多个各自带表头的片段。
    """按完整数据行切表，每片重复表头，同时满足行数和字符数上限。"""

    header = lines[:2]  # 前两行固定作为列名与分隔行。
    rows = lines[2:]  # 剩余部分才计入数据行数上限。
    if len(_piece_from_lines(header).content) > chunk_size:  # 先确认重复表头本身放得下。
        raise ValueError("表格表头长度超过片段长度上限 chunk_size")  # 不截断超长表头。
    if not rows:  # 表格可能只有列名，没有数据。
        return [_piece_from_lines(header)]  # 仍保留表头作为一个片段。

    pieces: list[_Piece] = []  # 保存已经完成的表格片段。
    group: list[_SourceLine] = []  # 当前片段累计的数据行，不包含表头。
    for row in rows:  # 按原表顺序依次分配每个数据行。
        # 表头加一行仍放不下时直接失败，不能强行截断单元格内容。
        one_row = _piece_from_lines((*header, row))  # *header 展开表头行，再附加完整数据行。
        if len(one_row.content) > chunk_size:  # 单行加表头超限，意味着无法按当前规则切分。
            raise ValueError("表格表头加一行数据的长度超过片段长度上限 chunk_size")  # 明确失败而非截断单元格。

        candidate = _piece_from_lines((*header, *group, row))  # 试算加入当前行后的完整片段长度。
        if group and (len(group) >= max_rows or len(candidate.content) > chunk_size):  # 旧组已满或新片段会超长。
            pieces.append(_piece_from_lines((*header, *group)))  # 提交旧数据组，并再次附上表头。
            group = [row]  # 当前行作为新数据组的第一行。
        else:  # 当前行仍能放入已有组。
            group.append(row)  # 保持原行顺序累积。
    if group:  # 循环末尾可能还有未提交的数据组。
        pieces.append(_piece_from_lines((*header, *group)))  # 补交最后一组，避免遗漏。
    return pieces  # 返回若干带完整表头的片段。


def _atomic_code_piece(
    lines: tuple[_SourceLine, ...], chunk_size: int  # 完整代码围栏行组及允许的最大字符数。
) -> _Piece:  # 合法代码块只能产生一个整体片段。
    """代码围栏整体保留（含围栏字符），超长则明确报错，不截断代码。"""

    piece = _piece_from_lines(lines)  # 将起止围栏及其代码内容整体拼接。
    if len(piece.content) > chunk_size:  # 围栏本身也占用长度预算。
        raise ValueError("围栏代码块长度超过片段长度上限 chunk_size")  # 不通过截断代码满足上限。
    return piece  # 合法代码块仅产生一个片段。

#正文切分和 overlap
def _split_prose(
    lines: tuple[_SourceLine, ...],  # 当前连续正文块的原文行。
    chunk_size: int,  # 主体与 overlap 合并后的字符上限。
    overlap: int,  # 从前一主体补入的最大字符数。
) -> list[_Piece]:  # 返回包含来源页码的正文片段列表。
    """先切正文主体，再从前一主体末尾补入不超过 overlap 的句子片段。"""

    annotated = _join_lines(lines)  # 拼接正文，同时建立字符到来源页码的映射。
    # 默认主体上限为 400 - 60 = 340，留出 overlap 空间，确保最终不超过 400。
    body_size = chunk_size - overlap  # 主体与 overlap 共用同一个最终长度上限。
    ranges = _recursive_ranges(annotated.text, 0, len(annotated.text), body_size, 0)  # 从首选分隔符开始切出主体区间。
    pieces: list[_Piece] = []  # 收集已经拼好 overlap 的正文片段。
    for index, current in enumerate(ranges):  
        source_ranges = [current]  # 至少需要追溯当前主体的来源页。
        content_parts = []  # 按输出顺序暂存 overlap 和主体文本。
        if index > 0 and overlap:  # 第一片没有前文；预算为 0 时也不补重叠。
            # 只读取前一主体，不读取已加过 overlap 的成品，避免重叠不断累积。
            previous = ranges[index - 1]  # 取前一个原始主体区间，不读取片段成品。
            overlap_range = _trailing_sentence_range(  # 选择前一主体末尾能放入预算的连续句段。
                annotated.text,  # 始终使用同一份原文，保证字符下标有效。
                previous[0],  # 前一主体起点，包含该字符。
                previous[1],  # 前一主体终点，不包含该字符。
                overlap,  # 最多允许补入的字符数，不保证一定补满。
            )
            if overlap_range is not None:  # 可能因末句太长而没有可用重叠内容。
                source_ranges.append(overlap_range)  # 将 overlap 所涉及的来源页也纳入追踪。
                content_parts.append(annotated.text[slice(*overlap_range)])  # slice(*区间) 等价于 [start:end]。
        content_parts.append(annotated.text[slice(*current)])  # 主体始终排在 overlap 后面。
        content = "".join(content_parts).strip()  # 直接拼接文本，只去掉成品两端空白。
        if content and not _HORIZONTAL_RULE_RE.fullmatch(content):  # 空片段和纯分隔线不输出。
            if len(content) > chunk_size:  # 内部兜底：主体加重叠理论上不能超过总预算。
                raise AssertionError("prose chunk exceeds the configured chunk_size")  # 超限表明算法内部约束被破坏。
            # 来源范围同时包含正文和补入的 overlap，页码不能漏掉重叠内容。
            page_start, page_end = _page_span(annotated.pages, source_ranges)  # 合并实际使用的全部来源区间。
            pieces.append(  # 将正文和页码范围一起保存。
                _Piece(content=content, page_start=page_start, page_end=page_end)  # 此时尚不生成 Chunk ID。
            )
    return pieces  # 返回按原文顺序排列的正文中间片段。


def _recursive_ranges(
    text: str,  # 原始拼接文本，递归过程中不复制改写。
    start: int,  # 当前范围起点，包含该位置字符。
    end: int,  # 当前范围终点，不包含该位置字符。
    chunk_size: int,  # 当前主体区间的最大跨度。
    separator_index: int,  # 从分隔符优先级列表的哪个位置开始尝试。
) -> list[tuple[int, int]]:  # 返回原文中的若干左闭右开区间。
    """返回原文中的左闭右开区间 [start, end)，便于保留文本和页码对应关系。"""

    trimmed = _trim_range(text, start, end)  # 先收缩区间两端空白，不改写原文。
    if trimmed is None:  # 当前范围没有非空白内容。
        return []  # 空白范围不生成片段。
    
    start, end = trimmed  # 使用收缩后的实际起止下标。
    if end - start <= chunk_size:  # 区间已经足够短，无需继续拆分。
        return [(start, end)]  # 返回单个左闭右开区间。
    if separator_index >= len(_PROSE_SEPARATORS):  # 所有自然分隔边界都已经尝试过。
        # 所有分隔符都无法满足长度限制时，才按字符数硬切。
        return [  # 最后退化为固定字符长度切分，跳过全空白子区间。
            trimmed_range  # 保存去除边缘空白后的有效区间。
            for offset in range(start, end, chunk_size)  # 每次前进一个主体长度。
            if (trimmed_range := _trim_range(text, offset, min(offset + chunk_size, end)))  
            is not None  # 只保留包含实际内容的结果。
        ]

#这种情况可能出现在没有标点、没有空格的超长字符串中。
    separator = _PROSE_SEPARATORS[separator_index]  # 取当前优先级的分隔符，例如段落换行。
    segments = _ranges_split_on(text, start, end, separator)  # 生成分隔后的原文区间列表。
    if len(segments) == 1:  # 仍然只有一段，当前分隔符不能有效细分。
        return _recursive_ranges(  # 保持当前范围，改用下一种更细的分隔符。
            text, start, end, chunk_size, separator_index + 1  # 只推进分隔符索引，不改变长度限制。
        )

    # 小段按顺序尽量合并；单段超长时递归使用下一种、更细的分隔符。
    result: list[tuple[int, int]] = []  # 存放已确定的输出主体区间。
    pending: tuple[int, int] | None = None  # 暂存可以继续和后段合并的区间。
    for segment in segments:  # 按当前分隔符切出的原文顺序遍历。
        if segment[1] - segment[0] > chunk_size:  # 当前单段仍超过主体上限。
            if pending is not None:  # 先提交它前面的短段，保证顺序不乱。
                result.append(pending)  # 保存已累积的有效区间。
                pending = None  # 清空合并缓存，避免跨过超长段合并。
            result.extend(  # 超长段递归后可能得到多个区间，逐个追加。
                _recursive_ranges(  # 在该段内部尝试更细的切分边界。
                    text,  # 始终复用原文，保持绝对下标。
                    segment[0],  # 仅递归处理当前超长段的起点。
                    segment[1],  # 仅递归处理当前超长段的终点。
                    chunk_size,  # 递归过程中不放宽主体长度上限。
                    separator_index + 1,  # 从下一种分隔符继续尝试。
                )
            )
            continue  # 超长段已经递归处理，不再走后面的短段合并逻辑。

        if pending is None:  # 还没有待合并的短段。
            pending = segment  # 当前段成为一个新主体的起始范围。
        elif segment[1] - pending[0] <= chunk_size:  # 合并后的原文跨度仍在预算内。
            pending = (pending[0], segment[1])  # 起点不变，终点延伸到当前段末尾。
        else:  # 再加入当前段就会超长。
            result.append(pending)  # 提交旧主体区间。
            pending = segment  # 以当前段开始下一个主体。
    if pending is not None:  # 循环结束时可能还有一个未提交主体。
        result.append(pending)  # 保存末尾内容。
    return result  # 返回有序区间，交给调用方提取文本和来源页码。


def _ranges_split_on(
    text: str, start: int, end: int, separator: str  # 原文、范围及本次使用的单个分隔符。
) -> list[tuple[int, int]]:  # 只返回区间，实际文本仍保留在原字符串中。
    """在指定原文范围内按分隔符切出区间，保留前段末尾的分隔符并跳过空白段。"""

    # 分隔符保留在前段中，避免切分时丢掉句末标点。
    ranges = []  # 保存当前分隔符切出的有效区间。
    cursor = start  # 游标从待处理范围的起点开始。
    while cursor < end:  # 仍有未扫描字符时继续查找。
        position = text.find(separator, cursor, end)  # 查找下一处分隔符；找不到返回 -1。
        segment_end = end if position == -1 else position + len(separator)  # 找到时把分隔符留在前段末尾。
        trimmed = _trim_range(text, cursor, segment_end)  # 收缩这一段的两端空白。
        if trimmed is not None:  # 纯空白段不加入结果。
            ranges.append(trimmed)  # 保存原文下标而不是复制字符串。
        if position == -1:  # 未找到更多分隔符，剩余文本已经处理完。
            break  # 结束扫描。
        cursor = segment_end  # 下一次从刚才分隔符之后开始。
    return ranges  # 返回当前分隔层级产生的全部有效区间。


def _trim_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    """仅收缩区间两端的空白，不改写区间内部的原文。"""
    while start < end and text[start].isspace():  # 检查区间左侧字符是否为空白。
        start += 1  # 起点右移，但不删除原文字符。
    while end > start and text[end - 1].isspace():  # 终点是排他的，因此检查 end - 1。
        end -= 1  # 终点左移，去除范围末尾空白。
    return None if start == end else (start, end)  # 收缩后为空则无有效区间。


def _trailing_sentence_range(
    text: str,  # 用于选取 overlap 的原文。
    start: int,  # 前一主体的起点。
    end: int,  # 前一主体的排他终点。
    max_chars: int,  # 允许补入的最大字符数。
) -> tuple[int, int] | None:  # 返回连续尾部区间；没有可选句段时返回 None。
    """从前一主体末尾向前选取连续句段；放不下就停止，不强行截成固定长度。

    句段按标点或换行识别；末尾无终止标点的文本也可能作为一个句段。
    因此 overlap 是最大预算，实际可少于该长度，甚至没有重叠。
    """

    matches = [match for match in _SENTENCE_RE.finditer(text, start, end)]  # 只在前一主体范围内识别句段。
    selected: list[re.Match[str]] = []  # 保存最终选中的连续尾部句段。
    total = 0  # 已使用的 overlap 字符预算。
    for match in reversed(matches):  # 从最后一个句段开始向前选择。
        if not match.group().strip():  # 忽略纯空白的匹配。
            continue  # 空白匹配不消耗句段长度预算。
        sentence_length = len(match.group())  # 统计完整匹配长度，包含匹配到的终止标点。
        if sentence_length > max_chars or total + sentence_length > max_chars:  # 单句或累积长度放不下。
            break  # 停止而非跳过，保证选取的是连续尾部，不拼接更早的零散短句。
        selected.insert(0, match)  # 逆序挑选、头部插入，恢复原文中的先后顺序。
        total += sentence_length  # 累加已选句段长度。
    if not selected:  # 末尾句段可能太长，导致完全没有可选重叠。
        return None  # 允许不添加 overlap，不强行截断末句。
    return _trim_range(text, selected[0].start(), selected[-1].end())  # 合并所选句段的整体原文区间。


def _join_lines(lines: tuple[_SourceLine, ...]) -> _AnnotatedText:
    """拼接原文，并为每个字符记录所属页；插入的换行归到后一行所在页。"""

    text_parts: list[str] = []  # 收集原文行和行间换行符。
    pages: list[int | None] = []  # 每添加一个字符，就添加一个对应页码。
    for index, line in enumerate(lines):  # 同时获得当前行及其顺序位置。
        if index:  # 首行前不插入换行，后续行之间补一个换行。
            text_parts.append("\n")  # 恢复 splitlines 移除后的行间分隔。
            pages.append(line.page_number)  # 新插入的换行归属于后一行所在页。
        text_parts.append(line.text)  # 保存这一行全部原文。
        pages.extend([line.page_number] * len(line.text))  # 按行字符数重复页码，建立一一映射。
    return _AnnotatedText(text="".join(text_parts), pages=tuple(pages))  # 文本和页码元组具有相同长度。


def _piece_from_lines(lines: tuple[_SourceLine, ...]) -> _Piece:
    """将完整行组装为片段，取这些行的最小、最大页码。"""

    content = "\n".join(line.text for line in lines).strip()  # 保留各行内容和内部换行，只去成品两端空白。
    numbers = [line.page_number for line in lines if line.page_number is not None]  # 过滤没有编号的行。
    return _Piece(  # 由完整行直接组装表格或代码等中间片段。
        content=content,  # 保存拼接后的正文。
        page_start=min(numbers) if numbers else None,  # 有页码则取最小值，否则不记录。
        page_end=max(numbers) if numbers else None,  # 有页码则取最大值，否则不记录。
    )


def _page_span(
    pages: tuple[int | None, ...], ranges: list[tuple[int, int]]  # 逐字符页码映射及实际使用的原文区间。
) -> tuple[int | None, int | None]:  # 返回最小、最大页码，或两个 None。
    """根据实际取用的字符区间回溯页码；无编号来源返回 (None, None)。"""

    numbers = {  # 使用集合去除同一页的重复编号。
        page  # 收集当前字符对应的来源页。
        for start, end in ranges  # 依次处理主体、overlap 等实际使用的区间。
        for page in pages[start:end]  # 在逐字符映射中读取该区间的页码。
        if page is not None  # 无编号来源不参与最小、最大页码计算。
    }
    if not numbers:  # 所有使用内容都来自无编号页面。
        return None, None  # 起止页必须同时缺省。
    return min(numbers), max(numbers)  # 返回主体与重叠内容共同涉及的页码范围。


def _stable_chunk_id(
    *,  # 身份字段必须显式按名称传入。
    document: KnowledgeDocument,  # 提供原文档 ID 和文档版本。
    index: int,  # 当前片段在整份文档输出列表中的位置。
    section_path: str,  # 当前片段所属的规范化章节路径。
    content: str,  # 当前片段的完整输出正文。
) -> str:  # 返回固定前缀加 SHA256 十六进制字符串。
    """由方案版本、文档 ID/版本、全局序号、章节和内容计算确定性 SHA256 ID。"""

    # 使用固定字段顺序与分隔符，不使用随机数或运行时间。
    payload = "\x1f".join(  # 用固定控制字符分隔各身份字段，再按固定顺序连接。
        (
            CHUNKING_SCHEME_VERSION,  # 区分不同分块算法方案版本。
            document.document_id,  # 区分不同来源文档。
            document.document_version,  # 区分同一文档的不同内容版本。
            str(index),  # 加入文档内全局序号；join 要求先转换为字符串。
            section_path,  # 将规范化章节位置纳入身份。
            content,  # 将当前片段实际正文纳入身份。
        )
    )
    return f"chunk-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"  # UTF-8 编码后计算 SHA256，并加固定前缀。
