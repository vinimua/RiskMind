# RiskMind Baseline 分块算法详解

> 对应代码：[`backend/src/risk_platform/knowledge/chunking.py`](../backend/src/risk_platform/knowledge/chunking.py)。
>
> 本文解释当前已实现的 V1 Baseline，不提出算法变更，也不描述 Parent-Child 构建。
> 公式采用标准 Markdown 数学语法：行内公式使用 `$...$`，独立公式使用 `$$...$$`；需使用支持 LaTeX 数学渲染的 Markdown 阅读器。

## 目录

1. [职责和完整处理链](#1-职责和完整处理链)
2. [输入、输出和中间结构](#2-输入输出和中间结构)
3. [参数、单位和数学记号](#3-参数单位和数学记号)
4. [公共入口与主流程](#4-公共入口与主流程)
5. [章节解析](#5-章节解析)
6. [正文、表格和代码识别](#6-正文表格和代码识别)
7. [正文递归切分](#7-正文递归切分)
8. [Overlap 选择与正文组装](#8-overlap-选择与正文组装)
9. [表格与代码切分](#9-表格与代码切分)
10. [页码来源追踪](#10-页码来源追踪)
11. [稳定 Chunk ID](#11-稳定-chunk-id)
12. [完整示例](#12-完整示例)
13. [校验、失败边界和实现限制](#13-校验失败边界和实现限制)
14. [全部函数速查](#14-全部函数速查)
15. [复习清单](#15-复习清单)

## 1. 职责和完整处理链

一句话概括：**先按文档结构拆，再按长度切；通过原文区间保留来源，最后补齐元数据并生成稳定 ID。**

输入是已经解析好的 `KnowledgeDocument`，输出是 `list[KnowledgeChunk]`。

```text
KnowledgeDocument：整份文档
        ↓ _split_sections()
_Section：按标题识别的章节
        ↓ _split_blocks()
_Block：正文 / 表格 / 代码
        ↓ 使用各自的切分算法
_Piece：文本 + 来源页码
        ↓ 补齐文档元数据、计算稳定 ID
KnowledgeChunk[]：可供持久化和向量化的片段
```

本模块不读取 PDF 或 Word，不调用 LLM 或 Embedding，不访问 PostgreSQL 或 Milvus，也不执行检索。

“确定性”表示：给定相同的文档对象与算法参数，反复运行产生相同的片段内容、顺序和 ID。确定性不等于“文档修改后所有旧 ID 都保持不变”。

## 2. 输入、输出和中间结构

### 2.1 `DocumentPage`：一页输入文本

```python
class DocumentPage(BaseModel):
    content: _NonBlankString
    page_number: int | None = Field(default=None, gt=0)
```

- `content`：解析器提供的本页 Markdown 文本，不能是空串或纯空白。
- `page_number`：可选页码；提供时必须大于零。
- 普通 Markdown 通常作为一个无页码页面传入。

这里的“页”由上游解析器定义，并不是本模块根据文本长度自动分页。

### 2.2 `KnowledgeDocument`：完整文档

| 字段 | 含义 |
|---|---|
| `document_id` | 文档的稳定业务标识 |
| `document_version` | 当前文档内容版本 |
| `document_type` | 模型开发报告或异常调查报告 |
| `title` | 文档级标题 |
| `source_uri` | 带协议、非 `file://` 的来源地址 |
| `model_id` | 关联的风控模型；通用知识可以不提供 |
| `pages` | 按原文顺序排列的页面元组，至少包含一页 |

模型使用 `extra="forbid"` 拒绝未知字段，使用 `frozen=True` 禁止实例字段重新赋值。

页码必须满足两种形式之一：

```python
[None, None, None]  # 全部无编号
[1, 2, 5]           # 全部有编号，唯一且严格递增
```

不允许混用 `[1, None, 3]`，也不允许重复或逆序。页码可以不连续。

若所有页面都有页码，则对任意相邻页面有：

$$
p_i < p_{i+1}
$$

### 2.3 内部结构

内部结构使用 `@dataclass(frozen=True)`，主要承载算法中间状态，不是数据库表。

| 结构 | 字段 | 作用 |
|---|---|---|
| `_SourceLine` | `text`、`page_number`、`in_fence` | 记录一行原文、该行对应的页码及是否属于代码区 |
| `_Section` | `path`、`lines` | 保存章节标题路径及正文行 |
| `_Block` | `kind`、`lines` | 区分 `prose`、`table`、`code` |
| `_AnnotatedText` | `text`、`pages` | 保存拼接正文及逐字符页码映射 |
| `_Piece` | `content`、`page_start`、`page_end` | 保存切分结果，尚未附上完整文档身份 |

`_SourceLine` 的“行”来自 `splitlines()`，是实际文本换行，不是编辑器窗口变窄导致的视觉折行。

`_Piece.content` 不是必然的一行。它可以是多行正文、含 overlap 的正文、带重复表头的表格片段，或包含围栏的完整代码块。

_SourceLine内部结构：原文行及其页码；标记代码围栏内的行，避免将代码中的 # 误识别为标题。

### 2.4 `KnowledgeChunk`：最终输出

每个 Chunk 包含：

```text
片段身份：chunk_id
文档身份：document_id、document_version、document_type
展示信息：title、section_path
实际正文：content
关联模型：model_id
来源信息：source_uri、page_start、page_end
```

`section_path` 允许为空；`content` 不能是纯空白。

有页码时要求：

$$
\text{page\_start} \leq \text{page\_end}
$$

无编号来源的起止页均为 `None`，不能只缺一端。

QA提纲

一句话区分；再说为什么要分开。

`title` 和 `section_path` 不能合并，因为它们语义不同：

- `title`：**文档级名称**，例如“credit_v3 模型开发报告”
- `section_path`：**Chunk 在文档里的位置**，例如“4 模型评估 / 4.2 评估结果”

分开后可以分别用于**文档过滤、章节定位、引用展示**和 Parent-Child 校验。

最终展示时可以拼成：

```
credit_v3 模型开发报告 / 4 模型评估 / 4.2 评估结果
```

所以原则是：

> **存储分开，展示可合并。**

QA:

① `source_uri` 是什么；② 为什么 Chunk 里也要保存；③ 在 RiskMind 里具体有什么用。

`source_uri` 的作用可以简单理解成：

> **告诉你这个 Chunk 最终是从哪一份原始文档来的。**

比如：

```
source_uri = "knowledge://reports/credit_v3_model_report"
```

或者以后可能是：

```
source_uri = "s3://bucket/path/report.pdf"
source_uri = "https://internal-wiki/.../credit_v3"
```

它主要有几个用途：

- **追溯来源**：检索命中一个 Chunk 后，可以知道它对应哪份原始材料。
- **引用展示**：后面生成 citation 时，可以把“这段内容来自哪里”展示给用户。
- **审计**：RiskMind 做企业级风控调查时，需要能回答“这条知识依据来自哪份文档”。
- **一致性校验**：Parent-Child 解析时，你现在也会检查 Child 和 Parent 的 `source_uri` 是否一致，防止把不同来源的内容错误关联起来。
- **后续打开原文**：未来前端可以根据 `source_uri` 跳回原始报告或知识页面。

为什么 Chunk 里也要保存，而不是只在 Document 表里保存？

因为 Chunk 被检索出来后，最好它自己就带完整 provenance：

```
Chunk
→ document_id
→ document_version
→ source_uri
```

这样不用每次再额外查一次 Document 才知道来源。

所以一句话总结：

> `source_uri` 保存的是**原始知识来源地址，用于追溯、引用、审计和跳转原文**。

## 3. 参数、单位和数学记号

```python
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 60
DEFAULT_TABLE_MAX_ROWS = 10
CHUNKING_SCHEME_VERSION = "v1"
```

定义以下符号：

| 符号 | 对应代码 | 含义 |
|---|---|---|
| $C$ | `chunk_size` | 成品片段字符上限 |
| $O$ | `overlap` | 正文重叠字符预算 |
| $B$ | `body_size` | 主体字符上限 |
| $R$ | `table_max_rows` | 表格片段最大数据行数 |
| $T$ | `annotated.text` | 当前正文块拼接后的原文 |
| $[s,e)$ | `(start, end)` | 左闭右开的原文区间 |

默认参数：

$$
C = 400,\qquad O = 60,\qquad R = 10
$$

参数必须满足：

$$
C > 0,\qquad 0 \leq O < C,\qquad R > 0
$$

正文给 overlap 预留空间：

$$
B = C - O = 400 - 60 = 340
$$

长度使用 Python `len(text)`，不是 token 数、UTF-8 字节数或屏幕显示宽度。

区间长度为：

$$
\lvert T[s:e] \rvert = e - s
$$

**即使第一片没有 overlap，主体仍按 $B=340$ 的上限切分。** 表格和代码直接使用成品预算 $C$，不使用正文主体预算 $B$。


QA:

① `table_max_rows` 控制什么；② 表格还有没有别的限制。

不是只有行数。

当前代码里表格至少受 **两层限制**：

- `table_max_rows`：每个表格 chunk 最多包含多少条**数据行**，默认是 `10`
- `chunk_size`：整个表格 chunk 的**字符长度上限**，默认是 `400`

而且表头会在每个分片里重复。实际切分逻辑是：

```
表头
+
最多 table_max_rows 条数据行
+
总字符数不能超过 chunk_size
```

如果出现：

```
表头本身 > chunk_size
```

或者：

```
表头 + 单独一行 > chunk_size
```

会直接报错，不会强行切这一行。

所以更准确地说：

> `table_max_rows` 只负责“行数上限”，但表格整体还同时受 `chunk_size` 限制。

QA:那为什么还要这个：`table_max_rows`：每个表格 chunk 最多包含多少条**数据行**  ？直接统计全部的字符不好吗‘?提纲

① 为什么不只看字符数；② `table_max_rows` 实际解决什么；③ 你这个项目里是否真的需要它。

可以只按字符数切，**技术上完全可以**。`table_max_rows` 不是硬性必须，它是一个额外的结构约束。

只看 `chunk_size=400` 会有一种情况：

```
每行都很短
```

例如：

```
| 特征 | PSI |
|---|---|
| age | 0.01 |
| income | 0.02 |
| debt | 0.03 |
...
```

可能 20～30 行加起来仍不到 400 字符。

这样一个 chunk 里会塞很多记录。虽然字符数没超，但从检索角度：

```
一个 chunk
→ 包含太多独立数据项
→ 语义比较杂
```

`table_max_rows=10` 就是在限制这种情况：

```
字符很短也不能无限塞行
```

所以两个限制分别控制：

```
chunk_size
→ 文本体积

table_max_rows
→ 表格记录数量
```

当前代码也是“谁先达到就切”：达到最大行数，或者再加一行会超过 `chunk_size`，都会结束当前表格片段。

质疑是成立的：

> 对 RiskMind 当前 V1 来说，`table_max_rows` 更像一个经验性保护参数，不是不可缺少的算法条件。

如果你想让规则更简单、更少经验参数，我其实倾向于：

```
只保留 chunk_size
+ 保证不拆单行
+ 每片重复表头
```

然后通过 6E benchmark 看表格检索效果。

也就是说，**`table_max_rows` 可以删，但删之前要确认它没有在防止“很多短行挤进一个 chunk”这个问题。**

QA:

① 什么叫“独立数据项”；② 为什么会“语义比较杂”；③ 举个例子。

意思是：**一个表格 chunk 里可能同时塞进很多彼此不同的事实。**

比如这个表：

```
| 特征 | PSI |
| age | 0.01 |
| income | 0.02 |
| debt_ratio | 0.18 |
| channel_code | 0.25 |
| apply_cnt_30d | 0.31 |
| login_days_30d | 0.05 |
...
```

如果 30 行都放进一个 chunk，那么这个 chunk 同时包含：

```
age 的稳定性
income 的稳定性
debt_ratio 的稳定性
channel_code 的稳定性
apply_cnt_30d 的稳定性
...
```

这些就是很多“独立数据项”。

当用户问：

```
apply_cnt_30d 的 PSI 是多少？
```

Embedding 检索时，整个 chunk 的向量是由这 30 行共同决定的。

于是：

```
apply_cnt_30d
```

只占这个 chunk 很小一部分，其他 29 行也在影响向量表示。

这就是我说的：

```
一个 chunk
→ 包含太多独立事实
→ chunk 的主题不够集中
```

所谓“语义比较杂”，不是说内容错误，而是：

> **这个 chunk 不再只代表一个比较明确的主题，而是混合了很多不同记录。**

但你这里要注意：这只是 `table_max_rows` 存在的一个理由，不代表“10 行一定最好”。这个 `10` 本身仍然是经验参数。



## 4. 公共入口与主流程

### 4.1 `chunk_document()`

```python
def chunk_document(document: KnowledgeDocument) -> list[KnowledgeChunk]:
    return _chunk_document_v1(
        document,
        chunk_size=DEFAULT_CHUNK_SIZE,
        overlap=DEFAULT_CHUNK_OVERLAP,
        table_max_rows=DEFAULT_TABLE_MAX_ROWS,
    )
```

业务代码调用这个固定参数的公共入口。内部函数支持显式参数，便于算法测试。

### 4.2 `_chunk_document_v1()`

处理步骤：

1. 校验长度、overlap 和表格行数参数。
2. 调用 `_split_sections()`，按顺序取得有效章节。
3. 用 `" / ".join(section.path)` 生成章节路径字符串。
4. 调用 `_split_blocks()`，识别章节内的内容块。
5. 根据类型调用 `_split_table()`、`_atomic_code_piece()` 或 `_split_prose()`。
6. 按原文顺序收集 `(section_path, piece)` 到 `pending`。
7. 用 `enumerate(pending)` 为片段生成从零开始的文档内全局序号。
8. 补齐文档元数据，计算 Chunk ID，创建 `KnowledgeChunk`。

```python
pending = [
    ("调查过程 / 标签成熟度", piece_1),
    ("调查过程 / 标签成熟度", piece_2),
    ("验证结果", piece_3),
]
```

`index` 是片段在文档输出中的顺序，不是检索名次 `rank`。

调用中的 `document=document`，左边是被调用函数的参数名，右边是当前函数持有的整份 `KnowledgeDocument` 对象；它不是文件路径，也不是当前 Piece。

## 5. 章节解析

### 5.1 `_split_sections()` 的状态

| 状态变量 | 含义 |
|---|---|
| `sections` | 已提交的有效章节 |
| `headings` | 当前标题层级到标题文字的映射 |
| `current_path` | 当前章节路径 |
| `current_lines` | 当前章节尚未提交的原文行 |
| `fence` | 当前打开的代码围栏；代码区外为 `None` |
| `document_heading_seen` | 是否已经遇到第一个一级标题 |

函数按页面顺序、页面内行顺序扫描。章节和围栏状态定义在页循环外，因此可以跨页延续。

### 5.2 标题处理

```markdown
# 异常调查报告
## 3 调查过程
### 3.1 标签成熟度
```

形成路径：

```python
("调查过程", "标签成熟度")
```

显示为：

```text
调查过程 / 标签成熟度
```

规则：

- 第一个一级标题被当作文档标题跳过，不进入路径或正文。
- 这里不比较该标题文字与 `document.title` 是否一致。
- 新标题出现时，先调用内部 `flush()` 提交前面的正文。
- 保留上级标题，删除旧的同级及更深层标题，再加入当前标题。
- `_normalize_heading()` 清理标题两端空白，并去除匹配的前导数字编号。
- 标题行不作为正文重复保存。

### 5.3 章节排除与有效内容

排除标题：`修订历史`、`目录`、`文档说明`。

`_is_excluded_section()` 检查整个路径，只要任意一级命中，当前章节就被排除。因此 `文档说明 / 使用方式` 也会排除。

`_has_meaningful_content()` 检查是否至少有一行非空且不是水平分隔线。它是存在性判断，并不负责逐行删除所有分隔线。

内部 `flush()` 无论是否保留章节，最后都会清空 `current_lines`，避免内容流入下一个章节。

### 5.4 代码围栏与注释

代码区内的 `#` 不识别为标题，`|` 不识别为表格内容。

当前实现用围栏的字符种类区分反引号和波浪号，并据此识别关闭，不是完整的通用 Markdown 解析器。

每页文本中的 HTML 注释先通过正则替换删除，再逐行扫描。这一步发生在围栏识别之前；不要理解成“代码中的 HTML 注释一定原样保留”。正则逐页执行，也不维护跨页面的 HTML 注释状态。

所有页面结束后，如果围栏仍未关闭，则抛出 `ValueError`；随后补交最后一个章节。

## 6. 正文、表格和代码识别

`_split_blocks()` 按以下优先级扫描章节行：

```text
围栏代码 → Markdown 表格 → 普通正文
```

正文暂存在 `prose`。遇到代码或表格时，内部 `flush_prose()` 先提交此前正文，保证输出顺序与原文一致。

**逐行检查**

```
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

```



### 6.1 代码块

从开始围栏读取到同类结束围栏，全部收集为一个 `code` 块，包括两端围栏行。

### 6.2 表格块

`_starts_table()` 至少需要两行：

```markdown
| 指标 | 数值 |
|---|---|
```

同时要求：

- 两行都不属于代码区。
- 列名行包含 `|`。
- 下一行通过 `_is_table_separator()` 检查。

表格分隔单元格至少含三个短横线，两边可带对齐冒号，如 `---`、`:---:`、`---:`。

数据行持续收集，直到遇到代码行、空行或不含 `|` 的行。停止行不会被本次表格扫描消费，会交给外层循环继续识别。

### 6.3 正文块

其余行进入 `prose`，循环结束后补交最后一段。

**不同章节之间不合并；同一章节中被表格或代码隔开的两段正文，也不会跨块合并或添加 overlap。**

## 7. 正文递归切分

### 7.1 `_recursive_ranges()` 的职责

只负责生成主体区间，不添加 overlap，不返回最终 Chunk。

```python
_recursive_ranges(text, start, end, chunk_size, separator_index)
```

其中 `chunk_size` 在正文调用场景下对应主体预算 $B$，不是成品预算 $C$。

`separator_index` 是分隔符列表中的位置，不是文本字符下标。

### 7.2 分隔符优先级

```python
_PROSE_SEPARATORS = (
    "\n\n", "\n", "。", "！", "？", "!", "?",
    "；", ";", "，", ",", " ",
)
```

```text
0：段落空行
1：换行
2：中文句号
后续：感叹号、问号、分号、逗号、空格
```

首次从索引零开始。当前分隔符无法有效拆开，或切出的某段仍超长时，使用 `separator_index + 1` 继续递归。

### 7.3 五步流程

```text
收缩两端空白
    ↓
纯空白？返回空列表
    ↓
长度已合适？返回整个区间
    ↓
分隔符用尽？按字符长度硬切
    ↓
按当前分隔符拆段：超长段递归，短段尽量合并
```

#### 第一步：收缩空白

`_trim_range()` 移动起止下标，不改写原文。收缩后没有内容则返回 `None`，递归函数返回空列表。

#### 第二步：短段直接返回

若当前区间满足：

$$
e-s \leq B
$$

则直接返回 `[(s, e)]`，不再尝试分隔符。

#### 第三步：分隔符用尽，硬切

对于没有可用分隔符的长文本，以 $B$ 为步长生成子区间：

$$
[s+kB,\ \min(s+(k+1)B,e))
$$

其中 $k$ 从零开始，直到覆盖原范围；每个子区间还会收缩空白，纯空白区间被跳过。

#### 第四步：按当前分隔符拆分

`_ranges_split_on()` 使用 `text.find()` 查找分隔符，将分隔符保留在前段末尾，再收缩两端空白，返回原文区间。

例如按句号处理 `甲甲。乙乙。`，得到 `甲甲。` 和 `乙乙。`，不会直接丢掉句号。

若只得到一段，则保持范围不变，换下一种分隔符。

#### 第五步：超长段递归，短段合并

核心变量：

```python
result = []     # 已经提交的输出区间
pending = None  # 尚可继续与下一段合并的暂存区间
```

| 当前情况 | 操作 |
|---|---|
| 当前段自身超长 | 提交旧 pending，清空缓存，再递归细切当前段 |
| 当前段不超长，pending 为空 | 当前段进入 pending |
| 当前段能与 pending 合并 | 保持起点，向后延长终点 |
| 合并会超长 | 提交旧 pending，以当前段开始新组 |
| 循环结束 | 补交剩余 pending |

设 pending 为 $[s_p,e_p)$，当前段为 $[s_c,e_c)$，合并条件为：

$$
e_c-s_p \leq B
$$

合并后的区间为：

$$
[s_p,e_c)
$$

这里计算的是原文整体跨度，段间空白也计入长度，并非仅相加两个去空白字符串的长度。

**超长段递归得到的结果直接加入 result，不再与外层 pending 重新合并。不存在最后一次全局“装满”处理。**

```python
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
```

### 7.4 递归实例

仅为演示，将主体预算设为 $B=6$：

```python
text = "甲甲。乙乙。丙丙丙丙丙丙。丁丁。"
```

按句号先拆出：

| 内容 | 长度 | 原文区间 |
|---|---|---|
| 甲甲。 | 3 | `[0, 3)` |
| 乙乙。 | 3 | `[3, 6)` |
| 丙丙丙丙丙丙。 | 7 | `[6, 13)` |
| 丁丁。 | 3 | `[13, 16)` |

前两句合并为 `[0, 6)`。第三句超长，先提交前两句，再递归；后续规则无法拆开时硬切。结果为：

```python
[(0, 6), (6, 12), (12, 13), (13, 16)]
```

对应文本：

```text
甲甲。乙乙。
丙丙丙丙丙丙
。
丁丁。
```

这个例子说明：当前硬切兜底可能留下很短的标点片段，这是现有行为。该参数仅用于理解算法，不代表修改公共 Baseline 的默认配置。

## 8. Overlap 选择与正文组装

### 8.1 `_split_prose()` 的步骤

1. `_join_lines()` 拼接正文并建立页码映射。
2. 计算主体预算 $B=C-O$。
3. `_recursive_ranges()` 返回主体区间。
4. 对非首个主体，从前一主体末尾选择 overlap。
5. 连接 overlap 与当前主体，清理成品两端空白。
6. 跳过空内容和仅含水平分隔线的成品。
7. 检查最终长度，计算页码，生成 `_Piece`。

```text
原主体：A        B        C
成品：  A    A尾部+B    B尾部+C
```

设第 $i$ 个主体文本为 $M_i$，选中的前文尾部为 $U_i$，则：

$$
P_0 = \operatorname{strip}(M_0)
$$

$$
P_i = \operatorname{strip}(U_i \Vert M_i),\qquad i>0
$$

其中 $\Vert$ 表示直接字符串连接，没有额外插入空格或换行。没有可选尾部时，$U_i$ 为空串。

长度满足：

$$
|M_i| \leq C-O,\qquad |U_i| \leq O
$$

因此：

$$
|P_i| \leq |U_i|+|M_i| \leq O+(C-O)=C
$$

代码还使用 `AssertionError` 作为内部兜底，检测最终正文超长这种违反算法预期的情况。

```
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
```



### 8.2 两个定义的不同职责

| 定义 | 使用位置 | 任务 |
|---|---|---|
| `_PROSE_SEPARATORS` | `_recursive_ranges()` | 主体超长时选择切分边界 |
| `_SENTENCE_RE` | `_trailing_sentence_range()` | 为 overlap 识别尾部句段 |

主体切分可以细到分号、逗号、空格；overlap 的句段按句末标点或换行识别，不把逗号、分号、空格当作同等结束边界。

### 8.3 `_trailing_sentence_range()` 的选择规则

先识别前一主体内的句段，再从最后一句向前连续选择：

```python
for match in reversed(matches):
    ...
```

设从后向前考察的句段长度为 $\ell_1,\ell_2,\ldots$，每次只在满足下式时继续加入：

$$
\sum_{j=1}^{k}\ell_j \leq O
$$

一旦下一句放不下，立即停止，不跳过它去选择更早的短句。

例如预算为 60，末尾三句逆序长度为 25、20、30：选择前两句，实际 overlap 为 45；再加入第三句会达到 75，因此停止。

如果末句自己长 80，则不添加 overlap，不强行截出最后 60 字符。

实现细节：

- 忽略纯空白匹配。
- 用 `selected.insert(0, match)` 恢复原文顺序。
- 最后收缩选中整体区间两端的空白。
- 末尾没有终止标点的文本也可能作为句段；它不是语言学意义上的完整句子分析器。
- overlap 来自前一**原始主体**，不是前一成品，因此不会层层累积更早的 overlap。

  ```
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
  
  ```

  

## 9. 表格与代码切分

### 9.1 `_split_table()`

```python
header = lines[:2]
rows = lines[2:]
```

前两行是表头，剩余才是计数的数据行。

每片必须同时满足：

$$
\text{data rows per piece} \leq R
$$

$$
|\text{header + data rows}| \leq C
$$

处理顺序：

1. 表头本身超长，失败。
2. 没有数据行时，将表头作为一个片段返回。
3. 逐行检查“表头 + 单行”能否放下；放不下则失败。
4. 试算“表头 + 当前组 + 新行”。
5. 当前组行数已满或试算超长，提交旧组，新行开启新组。
6. 最后补交剩余组。

每个片段重复表头，目的是单独召回这一片时仍能理解各列。行数限制不保证每片恰好有 10 行，字符预算可能先耗尽。

### 9.2 `_atomic_code_piece()`

将包含开始围栏、代码正文和结束围栏的完整行组拼成一个 Piece。

$$
|\text{complete fenced code}| \leq C
$$

超长则失败，不截断、不拆分、不添加正文 overlap。围栏本身也计入字符数。

## 10. 页码来源追踪

### 10.1 为什么使用原文区间

如果只保存切好的字符串，往往难以知道它来自哪一页。当前实现用字符下标和来源映射连接原文与片段。

`_join_lines()` 维护：

```python
_AnnotatedText(
    text="甲乙\n丙丁",
    pages=(1, 1, 2, 2, 2),
)
```

对应关系：

| 字符下标 | 字符 | 页码 |
|---|---|---|
| 0 | 甲 | 1 |
| 1 | 乙 | 1 |
| 2 | 换行 | 2 |
| 3 | 丙 | 2 |
| 4 | 丁 | 2 |

插入的换行归到后一行所在页。始终满足：

$$
\operatorname{len}(T)=\operatorname{len}(\text{pages})
$$

### 10.2 正文片段页码

`_split_prose()` 的 `source_ranges` 同时包含主体和实际补入的 overlap 区间。

设区间集合为 $\mathcal{I}$，逐字符映射为 $g(j)$，则收集页码：

$$
\mathcal{P}=\{g(j)\mid [s,e)\in\mathcal{I},\ s\leq j<e,\ g(j)\neq\mathrm{None}\}
$$

当 $\mathcal{P}$ 非空时：

$$
\text{page\_start}=\min\mathcal{P},\qquad
\text{page\_end}=\max\mathcal{P}
$$

为空时两端均为 `None`。这保证 overlap 来自上一页时，不会只记录当前主体所在页。

### 10.3 表格和代码片段页码

`_piece_from_lines()` 按完整行拼接正文，并直接从这些行的非空页码中取最小值和最大值。

注意：`page_start/page_end` 表示范围，不是所有实际页码的完整列表，也不是原 PDF 中的精确坐标。

## 11. 稳定 Chunk ID

`_stable_chunk_id()` 按固定顺序连接：

```text
scheme version
document_id
document_version
index
section_path
content
```

设这六项分别为 $V,D,W,i,S,X$，分隔符 $\delta$ 对应 `"\x1f"`，则：

$$
Q=V\Vert\delta\Vert D\Vert\delta\Vert W\Vert\delta\Vert\operatorname{str}(i)\Vert\delta\Vert S\Vert\delta\Vert X
$$

$$
\text{chunk\_id}=\texttt{chunk-}\Vert\operatorname{hex}\left(\operatorname{SHA256}\left(\operatorname{UTF8}(Q)\right)\right)
$$

代码没有使用随机数、运行时间或数据库自增 ID，因此相同输入可重复构建。

必须记住：

- 不是只对正文计算哈希；同样文字出现在不同文档或位置，可以得到不同 ID。

- `index` 参与计算，前面插入片段可能改变后续 ID。

- 规范化后的 `section_path` 参与计算；改变章节规范化规则会影响身份。

- 标题、来源 URI、页码等没有全部进入哈希，不能把 Chunk ID 当作完整对象摘要。

- ID 的确定性不代替持久化层的文档版本不可变检查。

- 即使正文相同，只要文档、版本或位置不同，也可以得到不同 ID

  ```
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
  
  ```

  

## 12. 完整示例

### 12.1 构造输入

```python
from risk_platform.knowledge.chunking import (
    DocumentPage,
    KnowledgeDocument,
    chunk_document,
)
from risk_platform.knowledge.contracts import KnowledgeDocumentType

document = KnowledgeDocument(
    document_id="CASE-001",
    document_version="v1",
    document_type=KnowledgeDocumentType.INCIDENT_REPORT,
    title="异常调查报告",
    source_uri="knowledge://cases/CASE-001",
    model_id="credit_v3",
    pages=(
        DocumentPage(
            page_number=1,
            content=(
                "# 异常调查报告\n"
                "## 1 调查过程\n"
                "标签尚未成熟。\n"
                "## 2 验证结果\n"
                "复算后指标恢复。"
            ),
        ),
    ),
)

chunks = chunk_document(document)

for chunk in chunks:
    print(chunk.section_path, chunk.content, chunk.page_start, chunk.page_end)
```

预期正文输出：

```text
调查过程 标签尚未成熟。 1 1
验证结果 复算后指标恢复。 1 1
```

### 12.2 中间过程

1. 第一个一级标题作为文档标题跳过。
2. 两个二级标题规范化为 `调查过程`、`验证结果`。
3. 每个章节识别出一个正文块。
4. 正文均未超过 340 字符，不再细切。
5. 每个正文块都只有一个主体，不添加 overlap。
6. 两个 Piece 的页码均为 `(1, 1)`。
7. 依次分配全局序号 0、1。
8. 使用相同文档身份、各自章节和正文计算两个 ID。
9. 补齐文档元数据，返回两个 `KnowledgeChunk`。

**配置 overlap 不代表不同章节之间也会互相复制正文。**

## 13. 校验、失败边界和实现限制

| 情况 | 当前行为 |
|---|---|
| 必填字符串为空或纯空白 | 校验失败 |
| 来源 URI 格式不符合约定，或使用 `file://` | 校验失败；不执行地址连通性验证 |
| 页码部分提供、部分缺省 | 校验失败 |
| 页码重复或逆序 | 校验失败 |
| 起止页只提供一端，或范围倒置 | 校验失败 |
| 长度或行数参数非法 | `ValueError` |
| 代码围栏未闭合 | `ValueError` |
| 表头超长 | `ValueError` |
| 表头加一行仍超长 | `ValueError` |
| 完整代码块超长 | `ValueError` |
| 最终正文违反长度约束 | 内部 `AssertionError` |
| 主体末句超出 overlap 预算 | 不添加 overlap，不报错 |
| 所有章节均被排除或没有有效内容 | 可以返回空列表 |

补充理解：

- 这是确定性 Markdown 规则处理，不是语义理解或完整 Markdown 标准解析器。
- 标题编号去除可能让同名章节具有相同路径；路径本身不是唯一 ID。
- overlap 按规则句段选择，不保证恰好 60 字符，也不保证语言学上的完整句。
- 递归硬切可能产生短片段，不会最后全局重新合并。
- 页码是来源范围；规范化、注释移除、换行拼接和边缘空白处理意味着输出并非原文件的逐字节切片。
- 这些行为属于当前 Baseline。修改切分规则、编号规则或身份字段，需要评估方案版本及已持久化数据，不能因阅读代码而顺手改算法。

## 14. 全部函数速查

共 26 个函数，包含模型校验方法和内部嵌套函数。

| 函数 | 核心职责 |
|---|---|
| `_validate_non_blank()` | 拒绝空字符串和纯空白，返回原值 |
| `_validate_source_uri()` | 校验带协议的非本地文件来源格式 |
| `KnowledgeDocument.validate_page_numbers()` | 校验文档页码完整性、唯一性和顺序 |
| `KnowledgeChunk.validate_page_span()` | 校验片段起止页是否成对且有序 |
| `chunk_document()` | 固定参数的业务入口 |
| `_chunk_document_v1()` | 调度章节、内容块、切分及最终包装 |
| `_split_sections()` | 按标题与围栏状态识别章节 |
| `_split_sections.flush()` | 提交当前有效且未被排除的章节 |
| `_normalize_heading()` | 去除标题边缘空白和匹配的编号 |
| `_is_excluded_section()` | 检查任意一级路径是否属于排除标题 |
| `_has_meaningful_content()` | 判断是否存在非空、非水平分隔线的行 |
| `_split_blocks()` | 识别正文、表格与围栏代码块 |
| `_split_blocks.flush_prose()` | 提交当前有效正文行缓存 |
| `_starts_table()` | 判断当前两行是否构成合法表头 |
| `_is_table_separator()` | 校验 Markdown 表格分隔行 |
| `_split_table()` | 重复表头，按完整数据行分组 |
| `_atomic_code_piece()` | 整体保留代码块，超长失败 |
| `_split_prose()` | 切主体、加 overlap、回溯页码 |
| `_recursive_ranges()` | 按优先级递归细切超长段，并合并短段 |
| `_ranges_split_on()` | 使用单个分隔符生成原文区间 |
| `_trim_range()` | 收缩区间两端空白 |
| `_trailing_sentence_range()` | 选择能放入预算的连续尾部句段 |
| `_join_lines()` | 拼接原文，并建立逐字符页码映射 |
| `_piece_from_lines()` | 将完整行组装为 Piece，并计算页码范围 |
| `_page_span()` | 根据多个实际使用区间回溯页码 |
| `_stable_chunk_id()` | 计算确定性片段身份 |

## 15. 复习清单

### 15.1 用一句话记住每一层

```text
Document：整份文档。
Line：原文一行，附带页码和代码标记。
Section：同一标题路径下的行。
Block：正文、表格或代码。
Piece：切好的内容和页码。
Chunk：Piece 加文档元数据和稳定 ID。
```

### 15.2 三条算法口诀

```text
结构：先分章节，再分类型。
长度：长段递归，短段合并，最后才加 overlap。
追溯：保留原文区间，计算页码，再生成稳定身份。
```

### 15.3 自测问题

1. 为什么正文主体不是 400，而是 340？
2. 为什么第一片也按 340 切？
3. `separator_index` 与文本 `start/end` 有什么区别？
4. `pending` 和 `result` 分别保存什么？
5. 遇到超长段前，为什么先提交 pending？
6. 递归结果为什么可能保留很短的片段？
7. 为什么末句长 80 时，不强行取 60 字符做 overlap？
8. 为什么读取前一主体，而不是前一成品？
9. 表格为什么每片重复表头？
10. 一个数据行加表头超长时会发生什么？
11. 插入换行对应哪一页？
12. overlap 来自上一页时如何追踪？
13. 在文档前面新增片段，为什么可能改变后面的 ID？
14. Chunk ID 是否能证明所有来源元数据都没有变化？

### 15.4 自测答案

1. 主体预算为 $B=C-O$，给 overlap 预留空间。
2. 所有主体共用同一次递归切分的 340 预算，不为第一片单独放宽。
3. 前者选择分隔规则，后两者定位原文字符区间。
4. pending 暂存可合并区间；result 保存已经提交的区间。
5. 保持原文顺序，避免跨过超长段合并。
6. 递归结果直接提交，不进行全局回填合并。
7. 连续句段放不下就停止，不截断末句来凑长度。
8. 防止 overlap 随片段不断累积。
9. 保证单独召回时仍能理解列含义。
10. 抛出 ValueError，不截断数据行。
11. 归到后一行所在页。
12. source_ranges 同时保存主体和 overlap 区间，统一取页码范围。
13. 文档内全局序号参与哈希。
14. 不能；标题、URI、页码等并未全部纳入哈希。
