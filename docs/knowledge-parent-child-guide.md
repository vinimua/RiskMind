# RiskMind Parent-Child 索引构建算法详解

> 对应代码：[`parent_child.py`](../backend/src/risk_platform/knowledge/indexing/parent_child.py)。
>
> 配套契约：[`indexing/contracts.py`](../backend/src/risk_platform/knowledge/indexing/contracts.py)。
> 底层分块规则：[`chunking.py`](../backend/src/risk_platform/knowledge/chunking.py)，详解见 [Baseline 分块学习文档](knowledge-chunking-guide.md)。
>
> 本文描述当前 `parent-child-v1` 的实际实现，不修改算法，也不将当前参数视为经过验证的最优值。
> 数学公式使用标准 LaTeX：行内 `$...$`，独立公式 `$$...$$`。阅读器需启用 Markdown 数学渲染。

## 目录

1. [先建立整体认识](#1-先建立整体认识)
2. [参数与数学记号](#2-参数与数学记号)
3. [输入与输出契约](#3-输入与输出契约)
4. [辅助函数与构建顺序](#4-辅助函数与构建顺序)
5. [Parent 范围算法](#5-parent-范围算法)
6. [Parent 范围映射回来源行](#6-parent-范围映射回来源行)
7. [构建全部 Parent](#7-构建全部-parent)
8. [在 Parent 内构建 Child](#8-在-parent-内构建-child)
9. [稳定 ID 的完整定义](#9-稳定-id-的完整定义)
10. [父子关联与契约校验](#10-父子关联与契约校验)
11. [可运行示例](#11-可运行示例)
12. [边界条件与容易误解的地方](#12-边界条件与容易误解的地方)
13. [与 Baseline 和在线检索的关系](#13-与-baseline-和在线检索的关系)
14. [复习与自测](#14-复习与自测)

## 1. 先建立整体认识

Parent-Child 将“搜索时的粒度”和“阅读时的粒度”分开：

| 对象 | 数据类型 | 主要用途 |
|---|---|---|
| Parent | `KnowledgeContextUnit` | 保存较完整、连续的阅读上下文 |
| Child | `KnowledgeChunk` | 保存适合搜索的小片段 |
| Link | `KnowledgeContextLink` | 明确 Child 属于哪个 Parent，以及父内顺序 |

构建方向严格为：

```text
KnowledgeDocument
    ↓ 按章节、原文范围划分
Parent[]
    ↓ 分别在每个 Parent 内切分
Child[]
    ↓ 同步建立关联
Child → Parent Link[]
    ↓
PreparedKnowledge
```

不是先执行 Baseline 产生 Child，再拼接得到 Parent。

本模块是纯构建层：不读写数据库，不生成 embedding，不执行搜索，不切换线上 route。
后续“命中 Child 后查 Parent”属于检索和 Context Resolution，不发生在此文件中。

## 2. 参数与数学记号

```python
PARENT_CHILD_SCHEME_VERSION = "v1"
PARENT_TARGET_SIZE = 1200
PARENT_MAX_SIZE = 1600
PARENT_OVERLAP = 0
CHILD_SIZE = 400
CHILD_OVERLAP = 60
```

| 符号 | 代码参数 | 含义 |
|---|---|---|
| $P_t$ | `PARENT_TARGET_SIZE` | 长章节正文细切与单元合并的目标长度 |
| $P_m$ | `PARENT_MAX_SIZE` | Parent 硬上限 |
| $C$ | `CHILD_SIZE` | Child 成品上限 |
| $O$ | `CHILD_OVERLAP` | Child 正文重叠预算 |
| $B$ | 底层 `body_size` | Child 正文主体预算 |
| $T$ | `annotated.text` | 当前章节预处理后拼接的文本 |
| $[s,e)$ | `(start, end)` | 原文字符区间，包含起点，不包含终点 |

当前配置：

$$
P_t=1200,\qquad P_m=1600,\qquad C=400,\qquad O=60
$$

Child 主体预算：

$$
B=C-O=340
$$

所有长度按 Python `len()` 计算，是字符数量，不是 token、字节或视觉显示宽度。

### 2.1 目标与硬上限的区别

```text
章节 1000 字符 → 可整体保留
章节 1400 字符 → 仍可整体保留
章节 1600 字符 → 仍可整体保留
章节超过 1600 → 使用单元切分与合并路径
```

单个完整表格或代码块可超过 1200，但不能超过 1600。
因此“Parent 每块都是 1200”是错误理解。

### 2.2 Parent overlap 的实际含义

`PARENT_OVERLAP=0` 声明当前策略不使用 Parent 重叠。
实现并没有读取这个常量执行通用 overlap 算法，而是通过不扩展已选区间实现无重叠。
单独将该常量改成其他数字不会自动改变行为。

参数是当前工程选择，不是公式推导的最佳值，也没有仅凭实现就得到效果验证。
修改规则必须评估 scheme/index profile 升级，不能静默修改已封板策略的语义。

## 3. 输入与输出契约

### 3.1 输入 `KnowledgeDocument`

包含文档 ID、文档版本、文档类型、标题、来源 URI、关联风控模型，以及页面文本。
原始输入已经由上游解析成 Markdown，本模块不直接解析 PDF 或 Word 文件。

### 3.2 输出 `PreparedKnowledge`

```python
PreparedKnowledge(
    search_chunks=tuple(children),
    context_units=tuple(parents),
    context_links=tuple(links),
)
```

它是完整构建结果包，不表示已经入库或已经可以在线搜索。

### 3.3 中间变量的区别

| 名称 | 保存什么 |
|---|---|
| `offsets` | 行对象到章节内字符起点的映射 |
| `units` | Parent 候选单元的字符区间 |
| `result` | 最终 Parent 的字符区间 |
| `parents` | 已创建的 Parent 对象 |
| `sources` | 与每个 Parent 一一对应的来源行 |
| `pieces` | 当前 Parent 内切出的 Child 中间片段 |
| `chunks` | 最终 Child 对象 |
| `links` | 最终父子关联 |

尤其要区分：**units 是候选单元；result 是 Parent 边界；parents 才是真正的 Parent 数据对象。**

## 4. 辅助函数与构建顺序

| 代码 | 职责 |
|---|---|
| `_identity()` | 生成方案隔离的确定性 Parent/Child ID |
| `_parent_ranges()` | 在一个章节中决定 Parent 字符边界 |
| `_parent_lines()` | 将字符边界转换成保留页码、围栏标记的行 |
| `ParentChildIndexBuildStrategy.build()` | 先完成全部 Parent，再构建 Child 和 Link |

依赖的 Baseline 底层函数包括：

```text
_split_sections()：章节识别与排除
_join_lines()：拼接正文，建立逐字符页码映射
_trim_range()：收缩区间两端空白
_split_blocks()：识别正文、表格、代码
_recursive_ranges()：递归切出正文主体区间
_split_prose()：正文主体加 overlap
_split_table()：表格按完整行拆片，重复表头
_atomic_code_piece()：代码整块保留
_page_span()：根据实际字符区间回溯页码
```

复用这些函数是为了保持相同 Markdown 解释和来源处理方式；没有调用 `chunk_document()` 先生成 Child。

## 5. Parent 范围算法

函数签名：

```python
def _parent_ranges(section: parsing._Section) -> list[tuple[int, int]]:
```

输入一个章节，只返回字符区间列表，不创建 Parent，不计算 Parent ID。

### 5.1 拼接章节与收缩空白

```python
annotated = parsing._join_lines(section.lines)
whole = parsing._trim_range(annotated.text, 0, len(annotated.text))
if whole is None:
    return []
```

`annotated.text` 是章节全文，`annotated.pages` 记录每个字符所属页。
`_trim_range()` 只移动区间边界，不改写字符串内部内容。纯空白返回 `None`。

假设：

```python
T = "\n\n正文。\n"
```

Parent 选取的是两端空白收缩后的范围，而不是含全部边缘空行的范围。
去空白是当前规则，不是所有文档处理的必然要求；它会影响长度判断及进入 ID 的正文。

### 5.2 按结构识别内容块

```python
blocks = parsing._split_blocks(section.lines)
```

得到保持原文顺序的正文、表格和代码块。先识别语法结构，再用字符下标表达边界，
不是不看结构就每隔 1200 个字符机械切一刀。

### 5.3 建立行起点映射

```python
offsets = {}
cursor = 0
for line in section.lines:
    offsets[id(line)] = cursor
    cursor += len(line.text) + 1
```

设第 $i$ 行正文为 $L_i$，起点为 $a_i$，则：

$$
a_0=0
$$

$$
a_{i+1}=a_i+|L_i|+1
$$

加一表示行间换行符，与 `_join_lines()` 的拼接方式对应。
最后一行之后游标也会加一，但不再用于实际行定位，因此不影响有效起点。

例如：

```text
原文行：甲甲、乙乙乙
拼接后：甲甲\n乙乙乙
下标：  0 1 2 3 4 5
行起点：第一行 0；第二行 3
```

`id(line)` 是本次 Python 运行中的对象身份，用于区分同样文字在不同位置的行。
它只用于定位，不进入最终 Parent/Child ID，不影响重复运行的确定性。
这里成立的前提是 `_split_blocks()` 保留了传入的原文行对象。

### 5.4 将每个块转换为区间

```python
start = offsets[id(block.lines[0])]
end = offsets[id(block.lines[-1])] + len(block.lines[-1].text)
trimmed = parsing._trim_range(annotated.text, start, end)
```

块起点是第一行起点；块终点是最后一行起点加最后一行长度。
再收缩边缘空白，跳过无有效内容的范围。

表格或代码：

```python
if block.kind != "prose":
    if end - start > PARENT_MAX_SIZE:
        raise ValueError(...)
    units.append((start, end))
```

要求：

$$
e-s\leq P_m
$$

块作为完整单元加入 `units`，不在内部划 Parent 边界；超长明确失败。

正文：

```python
units.extend(parsing._recursive_ranges(
    annotated.text, start, end, PARENT_TARGET_SIZE, 0,
))
```

按 $P_t=1200$ 目标递归细切，优先段落、换行、句末标点等边界，必要时字符硬切。
此处只生成区间，没有添加 Child overlap。最后的 `0` 是分隔符优先级索引，不是正文起点。

### 5.5 不超过 1600 的整章直接返回

```python
if whole[1] - whole[0] <= PARENT_MAX_SIZE:
    return [whole]
```

设有效整章区间为 $[w_s,w_e)$，则：

$$
w_e-w_s\leq P_m\quad\Longrightarrow\quad\text{ParentRanges}=[(w_s,w_e)]
$$

注意实际执行顺序：代码先生成并检查内容单元，再进行整章判断。
短章节最终直接使用 `whole`，前面生成的细切 `units` 不再用于组合 Parent。

例如整章 1400 字符，即使正文曾按 1200 递归生成候选区间，最终仍返回整章一个 Parent。

### 5.6 长章节按顺序合并单元

```python
result = []
for start, end in units:
    if result and end - result[-1][0] <= PARENT_TARGET_SIZE:
        result[-1] = (result[-1][0], end)
    else:
        result.append((start, end))
return result
```

设最后一个 Parent 当前为 $[s_p,e_p)$，新单元为 $[s_u,e_u)$，满足下式才合并：

$$
e_u-s_p\leq P_t
$$

合并为：

$$
[s_p,e_u)
$$

这是原文整体跨度，单元间的空白也计入并保留，不只是两个正文长度相加。
放不下就提交为一个新的 Parent 区间；单个原子块允许大于 1200，但已受 1600 上限检查。

### 5.7 1802 字符的示意例子

假设章节内容为：

```text
正文 A：700 字符
换行：    1 字符
完整表格：300 字符
换行：    1 字符
正文 B：800 字符
```

总长：

$$
700+1+300+1+800=1802
$$

候选单元：

```python
units = [(0, 700), (701, 1001), (1002, 1802)]
```

组合过程：

| 新单元 | 判断 | 结果 |
|---|---|---|
| 正文 A `[0,700)` | 尚无 Parent | 新建 `[0,700)` |
| 表格 `[701,1001)` | $1001-0\leq1200$ | 合并成 `[0,1001)` |
| 正文 B `[1002,1802)` | $1802-0>1200$ | 新建 `[1002,1802)` |

最终：

```python
[(0, 1001), (1002, 1802)]
```

Parent 1 包含正文 A、换行和表格；Parent 2 包含正文 B。边界上的那一个换行不属于两个区间，
所以无重叠不等于所有输入字符都必须恰好属于某个 Parent。边缘空白和被过滤内容可以被省略。

## 6. Parent 范围映射回来源行

函数：

```python
def _parent_lines(section, start, end) -> tuple[parsing._SourceLine, ...]:
```

### 6.1 为什么不能只保留 Parent 字符串

普通字符串不包含每行原页码，也不包含 `in_fence`。
后续 Child 还要识别正文、表格和代码，并计算自己的页码，因此需要带来源的行。

```text
_parent_ranges()：决定边界
T[start:end]：取得 Parent 正文
_parent_lines()：取得 Parent 对应的带来源行
后续分块函数：在这些行中切 Child
```

### 6.2 换算行内切片

```python
line_end = cursor + len(line.text)
if cursor < end and line_end >= start:
    lines.append(parsing._SourceLine(
        text=line.text[max(0, start-cursor):min(len(line.text), end-cursor)],
        page_number=line.page_number,
        in_fence=line.in_fence,
    ))
cursor = line_end + 1
```

设当前行起点为 $a$、行长为 $\ell$，Parent 范围为 $[s,e)$，则保留候选行的条件为：

$$
a<e\quad\land\quad a+\ell\geq s
$$

行内起止位置：

$$
l=\max(0,s-a),\qquad r=\min(\ell,e-a)
$$

实际取 `line.text[l:r]`。

例如行起点 100、行长 50、Parent 范围 `[110,130)`：

$$
l=10,\qquad r=30
$$

所以取 `line.text[10:30]`，不能把整行复制进 Child 的来源。

实现使用 `line_end >= start`，行尾恰好等于 Parent 起点时也可能保留一个空切片。
这是当前行结构重建细节；不能擅自改成严格大于而假设行为不变。

原行的 `page_number`、`in_fence` 原样复制，只有文本按 Parent 范围切片。

## 7. 构建全部 Parent

入口：

```python
strategy = ParentChildIndexBuildStrategy()
prepared = strategy.build(document)
```

`name="parent-child-v1"` 标识策略。类本身没有长期积累 Parent 的实例状态；列表都在每次 `build()` 中新建。

### 7.1 准备容器和元数据

```python
parents = []
sources = []
metadata = document.model_dump(exclude={"pages"})
```

`metadata` 包含文档 ID、版本、类型、标题、来源 URI、模型标识，不重复携带完整页面列表。

`parents[i]` 与 `sources[i]` 一一对应，后者为该 Parent 的来源行。

### 7.2 遍历章节并创建 Parent

```python
for section in parsing._split_sections(document):
    annotated = parsing._join_lines(section.lines)
    for start, end in _parent_ranges(section):
        content = annotated.text[start:end]
        section_path = " / ".join(section.path)
        page_start, page_end = parsing._page_span(annotated.pages, [(start, end)])
        ...
```

复用 Baseline 标题规范化和章节排除规则，不跨章节建 Parent。
Parent 正文直接来自章节原文切片，而不是 Child 的拼接结果。
这里“原文”指经过章节解析、HTML 注释处理、标题剥离、行拼接后的 $T$，并非原 PDF 文件字节。

页面映射为 $g(j)$ 时，范围页码集合为：

$$
\mathcal{P}=\{g(j)\mid s\leq j<e,\ g(j)\neq\mathrm{None}\}
$$

非空时取最小和最大值，空集合则两端都为 `None`。

### 7.3 生成 Parent 身份并保存

```python
parent = KnowledgeContextUnit(
    **metadata,
    section_path=section_path,
    content=content,
    page_start=page_start,
    page_end=page_end,
    context_id=_identity(
        "parent", document.document_id, document.document_version,
        section_path, len(parents), content,
    ),
)
parents.append(parent)
sources.append(_parent_lines(section, start, end))
```

`**metadata` 是把字典展开为关键字参数。
创建当前 Parent 前的 `len(parents)` 是当前全局 Parent 序号，跨章节累计，从零开始。

这一整个外层循环结束时，所有 Parent 都已完成，尚未生成任何 Child。

## 8. 在 Parent 内构建 Child

### 8.1 Parent 与来源行成对处理

```python
chunks = []
links = []
for parent, lines in zip(parents, sources, strict=True):
    pieces = []
    ...
```

`zip()` 每轮取得一个 Parent 及它的来源行。`strict=True` 要求两个列表长度相同，
不会悄悄截断较长列表。每个 Parent 的 `pieces` 都重新创建。

### 8.2 按内容类型切 Child

| 内容块 | 调用 | 行为 |
|---|---|---|
| 代码 | `_atomic_code_piece(lines, 400)` | 完整围栏代码最多 400，超长失败 |
| 表格 | `_split_table(lines, 10, 400)` | 每片重复表头，最多 10 数据行且总长不超过 400 |
| 正文 | `_split_prose(lines, 400, 60)` | 主体最多 340，再添加前一主体末尾最多 60 的句段 |

不能因 Parent 能容纳 800 字符的代码，就认为构建一定成功：Child 代码预算仍是 400。
完整表格虽然在 Parent 中不拆，但生成 Child 时可以按完整数据行拆成多个小表片段。

### 8.3 正文 overlap

设第 $i$ 个 Child 主体为 $M_i$，从前一主体取得的尾部为 $U_i$，则：

$$
X_0=\operatorname{strip}(M_0)
$$

$$
X_i=\operatorname{strip}(U_i\Vert M_i),\quad i>0
$$

其中 $\Vert$ 表示直接字符串连接。

$$
|M_i|\leq340,\qquad |U_i|\leq60,\qquad |X_i|\leq400
$$

- overlap 来自前一原始主体，不读取前一成品，避免重复累积。
- 只在当前 Parent 的当前连续正文块内添加，不跨 Parent，也不跨表格或代码。
- 放不下完整尾部句段时可以不添加，不强行取固定 60 字符。
- Parent 正文已在第一阶段确定，不会被 Child overlap 改写。

### 8.4 父内序号 `enumerate()`

```python
for ordinal, piece in enumerate(pieces):
```

`enumerate()` 依次给出 `(序号, 元素)`，左边的两个变量对它解包：

```text
(0, piece_A)
(1, piece_B)
(2, piece_C)
```

`ordinal` 不是关键字，可以换其他变量名。这里强调的是 Child 在当前 Parent 内的顺序。
每进入一个新 Parent，序号重新从零开始；它不是检索排名，也不是文档内全局 Child 序号。

## 9. 稳定 ID 的完整定义

### 9.1 `_identity()` 的公共编码

```python
payload = json.dumps(
    ["parent-child", PARENT_CHILD_SCHEME_VERSION, kind, *parts],
    ensure_ascii=False,
    separators=(",", ":"),
)
return f"pc-{kind}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
```

`*parts` 收集调用方的可变位置参数，构建列表时再展开；JSON 数组保留字段边界和顺序。
`ensure_ascii=False` 保留中文编码形式，固定分隔格式去掉不必要的 JSON 空格。
没有随机数、时间戳和运行时行对象地址。

定义 $J$ 为上述紧凑 JSON 编码，$H$ 为 UTF-8 后的 SHA256 十六进制结果：

$$
H(A)=\operatorname{hex}\left(\operatorname{SHA256}(\operatorname{UTF8}(J(A)))\right)
$$

### 9.2 Parent ID

设文档 ID 为 $D$、文档版本为 $W$、章节路径为 $S$、全局 Parent 序号为 $j$、正文为 $P$：

$$
I_P=\texttt{pc-parent-}\Vert H([\texttt{parent-child},\texttt{v1},\texttt{parent},D,W,S,j,P])
$$

| 字段 | 作用 |
|---|---|
| 方案和版本 | 隔离不同构建语义 |
| `parent` | 与 Child 类型分开 |
| 文档 ID | 区分不同来源 |
| 文档版本 | 区分同文档的不同内容版本 |
| 章节路径 | 把结构位置纳入身份 |
| 全局 Parent 序号 | 区分相同文字的不同出现位置 |
| Parent 正文 | 内容变化时身份变化 |

这不是唯一可能的 ID 方案，也不是数学上的最小字段集，而是当前身份约定。

### 9.3 Child ID

设父内序号为 $k$，Child 正文为 $X_k$：

$$
I_{C_k}=\texttt{pc-child-}\Vert H([\texttt{parent-child},\texttt{v1},\texttt{child},I_P,k,X_k])
$$

代码对应：

```python
chunk_id = _identity("child", parent.context_id, ordinal, piece.content)
```

Child ID 绑定 Parent，同样的正文出现在不同 Parent 下会得到不同身份。
Parent 身份改变，其下 Child 身份也会改变。

### 9.4 身份设计的边界

- 相同输入与方案得到相同 ID，支持重复构建的一致性。
- 前面插入 Parent 可能改变后面的全局序号及 ID。
- 章节路径变化会影响 ID。
- ID 不包含完整元数据：标题、来源 URI、页码等没有全部显式纳入。
- 确定性 ID 不代替 repository 的原文版本不可变检查，也不是数据库防篡改机制。
- 当前 scheme/profile 的业务语义固定，参数调整不能依靠旧 ID 逻辑自动完成迁移。

## 10. 父子关联与契约校验

### 10.1 创建 Child

```python
chunks.append(KnowledgeChunk(
    **metadata,
    chunk_id=chunk_id,
    section_path=parent.section_path,
    content=piece.content,
    page_start=piece.page_start,
    page_end=piece.page_end,
))
```

文档元数据来自 `metadata`，章节来自 Parent，实际正文和页码来自 Child Piece。
Child 页码可以比 Parent 范围更小，但不能超出 Parent。

### 10.2 创建 Link

```python
links.append(KnowledgeContextLink(
    chunk_id=chunk_id,
    context_id=parent.context_id,
    child_ordinal=ordinal,
))
```

例子：

```text
C0 → P0，ordinal=0
C1 → P0，ordinal=1
C2 → P1，ordinal=0
```

Link 不保存正文，只明确归属和父内顺序，后续不需要从 ID 前缀或文本猜测关系。

### 10.3 `PreparedKnowledge` 的约束

在有 Parent/Link 的构建结果中，契约会检查：

1. Child ID 与 Parent ID 在各自集合中不重复。
2. 每条 Link 都引用实际存在的 Child、Parent。
3. 每个 Child 恰好有一条 Link。
4. 每个 Parent 至少有一个 Child。
5. 每个 Parent 的 Link 按顺序具有 `0, 1, ..., n-1` 的父内序号。
6. 父子文档 ID、版本、类型、标题、章节、模型、URI 一致。
7. 父子页码要么均无编号，要么 Child 页码落在 Parent 内。

对于有编号来源：

$$
p_s\leq c_s\leq c_e\leq p_e
$$

这里保护的是结构、来源和映射，不是在契约中重新实现全文分块，也不证明 Child 文本是 Parent 的逐字连续子串。
例如表格 Child 重复表头，不能简单要求它是 Parent 的一个连续原文切片。

纯 Baseline 的 Parent 和 Link 集合都为空时，走原契约兼容分支。

## 11. 可运行示例

以下示例从仓库已安装的 Python 项目环境运行。使用内部函数的示例仅用于理解算法，不是新增业务入口。

### 11.1 900 字符正文：一个 Parent、三个 Child

```python
from risk_platform.knowledge.chunking import DocumentPage, KnowledgeDocument
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.indexing.parent_child import ParentChildIndexBuildStrategy

document = KnowledgeDocument(
    document_id="CASE-DEMO-001",
    document_version="v1",
    document_type=KnowledgeDocumentType.INCIDENT_REPORT,
    title="调查报告",
    source_uri="knowledge://cases/CASE-DEMO-001",
    model_id="credit_v3",
    pages=(DocumentPage(content="# 调查报告\n## 调查过程\n" + "甲。" * 450),),
)

prepared = ParentChildIndexBuildStrategy().build(document)

assert len(prepared.context_units) == 1
assert len(prepared.context_units[0].content) == 900
assert [len(child.content) for child in prepared.search_chunks] == [340, 400, 280]
assert [link.child_ordinal for link in prepared.context_links] == [0, 1, 2]
assert all(link.context_id == prepared.context_units[0].context_id for link in prepared.context_links)

print("Parent 长度：", len(prepared.context_units[0].content))
print("Child 长度：", [len(child.content) for child in prepared.search_chunks])
```

这些长度由该示例的规则句段产生，不代表任意 900 字符正文一定切成同样三块。

本例主体长度为 340、340、220，后两片分别补入 60 字符：

$$
340,\quad 340+60=400,\quad 220+60=280
$$

Child 总字符数为 1020，包含重复 overlap，但 Parent 正文仍然只有 900 字符。

### 11.2 构造精确的 1802 字符混合章节

```python
from risk_platform.knowledge import chunking as parsing
from risk_platform.knowledge.indexing.parent_child import _parent_ranges

header = "|字段|值|\n|---|---|\n"
prefix, suffix = "|A|", "|"
table = header + prefix + "x" * (300 - len(header) - len(prefix) - len(suffix)) + suffix
assert len(table) == 300

text = "甲" * 700 + "\n" + table + "\n" + "乙" * 800
section = parsing._Section(
    path=("调查过程",),
    lines=tuple(parsing._SourceLine(text=line, page_number=None) for line in text.splitlines()),
)

assert len(text) == 1802
assert _parent_ranges(section) == [(0, 1001), (1002, 1802)]
print(_parent_ranges(section))
```

本例精确对应第 5 节的范围示意：先按结构识别正文与表格，再用原文跨度判断能否合并。

### 11.3 跨页 Parent 的来源行切片

```python
from risk_platform.knowledge import chunking as parsing
from risk_platform.knowledge.indexing.parent_child import _parent_lines

section = parsing._Section(
    path=("调查过程",),
    lines=(
        parsing._SourceLine(text="甲乙", page_number=3),
        parsing._SourceLine(text="丙丁", page_number=4),
    ),
)
annotated = parsing._join_lines(section.lines)
assert annotated.text == "甲乙\n丙丁"

lines = _parent_lines(section, 1, 4)
assert [line.text for line in lines] == ["乙", "丙"]
assert [line.page_number for line in lines] == [3, 4]
assert parsing._join_lines(lines).text == annotated.text[1:4] == "乙\n丙"
assert parsing._page_span(annotated.pages, [(1, 4)]) == (3, 4)
print([(line.text, line.page_number) for line in lines])
```

这说明 `_parent_lines()` 不是多余的字符串复制：它在保留边界内文本的同时恢复来源行属性。

## 12. 边界条件与容易误解的地方

| 情况 | 当前行为 |
|---|---|
| 整个章节为无效内容或已被章节规则排除 | 不生成对应 Parent |
| 有效章节长度不超过 1600 | 整章一个 Parent，不强制按 1200 拆开 |
| 长章节正文 | 按 1200 目标递归切分，再按原序组合单元 |
| 完整表格或代码超过 1600 | Parent 阶段失败 |
| 代码 800 字符 | Parent 阶段可能通过，Child 阶段因超过 400 失败 |
| 表头加一行超过 400 | Child 表格切分失败，不截断单元格 |
| Child 主体末句超过 60 | 不补该句 overlap，而不是强行截成 60 |
| 不同 Parent 中的 Child | 不跨父重叠，父内 ordinal 各自从零开始 |
| Parent 身份变化 | 绑定该 Parent 的 Child ID 随之变化 |
| 同一父内文本重复 | ordinal 区分其出现位置 |

### 12.1 “原文保留”不等于逐字节不变

Parent 保存所选 $T[s:e]$ 的内容，不通过 Child 拼接，也不添加 Child overlap。
但 $T$ 已经经过前置章节解析和行拼接，区间两端空白还可能被收缩。
空白敏感的代码缩进不能抽象地认为在所有边缘情况都完全不受处理影响。

### 12.2 两个上限必须分开记

```text
Parent 合法 ≠ Child 一定可构建
```

表格可在 Child 层按行拆，代码则始终作为完整原子块，因此较长代码是明确的支持边界。

### 12.3 参数不自动改变既有索引

修改 1200/1600/400/60 会影响边界、正文、Parent ID、Child ID 和映射。
不能只改一个常量就假设旧数据库、Milvus 或 serving snapshot 会自动同步。
后续应使用新方案身份和完整迁移/重建流程，具体上线由其他模块负责。

### 12.4 工程约束与效果结论分开

确定性、完整映射、来源一致和长度约束可以通过测试验证。
Parent 是否提高召回、答案正确性，以及 token 成本是否合理，需要真实语料和模型评测。
当前实现不是“参数已经最优”的证据，也没有实现自动合并阈值或多级层次树。

## 13. 与 Baseline 和在线检索的关系

### 13.1 与 Baseline 构建比较

```text
Baseline：
Document → Baseline Chunk
         → PreparedKnowledge.search_chunks

Parent-Child：
Document → Parent → Child
         → PreparedKnowledge.search_chunks / context_units / context_links
```

两种策略共享底层语法处理，但 Parent-Child 的父边界会影响 Child 切分及身份。
不能假定 Parent-Child Child 与直接 `chunk_document()` 的 Baseline Chunk 内容、数量或 ID 完全相同。

### 13.2 后续使用方式，不是本模块执行内容

```text
构建结果 → PG 保存 Child / Parent / Link
                 ↓
Milvus 搜索 Child
                 ↓
PG 校验命中与来源
                 ↓
通过 Link 查 Parent
                 ↓
按首次 Child rank 去重 Parent
                 ↓
KnowledgeContext[]
```

当前在线策略命中 Child 就解析对应 Parent，不采用“同父被召回 Child 比例超过阈值才展开”的 Auto-Merging 规则。
Context 和 KnowledgeHit 依然是知识线索，不自动成为当前案件的正式 Evidence。

## 14. 复习与自测

### 14.1 五句话记住核心

1. `_parent_ranges()` 决定边界，只返回字符位置。
2. `_parent_lines()` 将边界映射回带页码和围栏标记的行。
3. `build()` 第一轮创建全部 Parent，第二轮才切 Child。
4. Parent 无 overlap；Child 只在各自 Parent 的正文块内加 overlap。
5. Child ID 绑定 Parent，Link 明确记录归属与父内顺序。

### 14.2 常见问题与答案

**为什么不能只保存 Parent 字符串再切 Child？**

普通字符串没有逐行页码和围栏属性；当前通过来源行保证后续类型识别与 Child 页码追踪。

**为什么不每 1200 字符直接切一刀？**

避免 Parent 边界切断表格和代码，并允许不超过 1600 的章节整体保留。

**为什么已经细切了 units，短章节又整体返回？**

当前执行顺序先做单元准备和原子块检查，再选择短章节整体保留分支；最终该分支不使用候选细切结果。

**为什么需要 sources，parents 不够吗？**

Parent 对象保存正文和页码范围，sources 额外保存逐行来源属性，供 Child 切分使用。

**为什么 Parent 用 len(parents)，Child 用 ordinal？**

前者是跨章节累计的 Parent 全局序号，后者是每个 Parent 内从零开始的 Child 序号。

**相同 Child 正文是否一定有相同 ID？**

不是。Parent ID、父内 ordinal、方案版本也参与计算。

**为什么 Link 还保存 Parent ID，Child ID 不是已经绑定 Parent 了吗？**

哈希不可反向解析；关联查询需要显式字段，不能从 Child 哈希中恢复 Parent ID。

**Parent 原文会被 Child overlap 改写吗？**

不会。Parent 在第一阶段已完成，Child 只在自己的结果中复制尾部句段。

**构建成功意味着已经可在线搜索吗？**

不是。还需要权威持久化、向量物化、验收、显式激活和后续检索链路。

**读完本文件后接着看什么？**

先理解 `indexing/contracts.py` 的父子契约，再看 `models.py` 和 `repository.py`，
把纯构建结果连接到权威存储；不要把这里的纯算法误解成完整 ingestion 或 retrieval。
