# Knowledge Context Resolution：算法与 Python 语法详解

本文对应 `backend/src/risk_platform/knowledge/context_resolution.py`，按当前代码解释输入输出、完整算法、数据校验和 Python/SQLAlchemy/Pydantic 语法。

公式使用 `$...$` 和 `$$...$$`，需在支持 LaTeX 数学公式的 Markdown 阅读器中查看。ID、页码与正文示例用于教学，不代表真实数据。

## 1. 先用一句话定位职责

本文件把已经检索到的 Child 转换成最终阅读上下文：Baseline 返回 Child 正文；Parent-Child 通过 Link 找到 Parent，返回 Parent 原文。

```text
retrieval.py：问题 → 向量搜索 → PG 校验 → Child Hit
                                               ↓
context_resolution.py：Child → 最终 Context
                                               ↓
后续生成流程：问题 + Context → 答案
```

本文件只查询 PostgreSQL，不调用 Embedding，不搜索 Milvus，不重新查线上路由，不检查 READY，也不生成答案。

## 2. 参与的五类对象

| 对象 | 身份与职责 |
|---|---|
| Child | 被向量搜索命中的小块，保存于 PG 的 KnowledgeChunkRow |
| Hit | 一次检索中的命中记录，包含 Child 来源、正文和 rank |
| Link | Child→Parent 显式映射，含 child_ordinal |
| Parent | 完整原始上下文，保存于 PG 的 KnowledgeContextRow |
| Context | 供后续阅读/生成的统一结果，包含正文与 supporting_hits |

Child 与 Hit 不完全相同：Child 是持久化知识对象；Hit 额外表达“它在这次检索中排第几”。

Parent 与 Context 也不同：Parent 是存储中的父块，Context 是在线输出契约。Baseline 同样会输出 Context，但没有 Parent。

## 3. 输入结构

```text
DenseChildRetrievalResult
├── snapshot
│   ├── index_profile
│   ├── collection
│   └── route_version
└── hits
    └── RetrievedKnowledgeHit
        ├── hit: KnowledgeHit
        │   ├── source: document_id / document_version / chunk_id
        │   ├── document_type / model_id
        │   ├── title / section_path / content
        │   └── rank
        └── score
```

本阶段提取 `item.hit`，不对 score 执行聚合或重排。外层结果重新校验会执行已有数据契约，但本模块不会重复整个 retrieval 的分数降序检查。

## 4. 输出结构

```python
class ContextResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: RetrievalRouteSnapshot
    contexts: tuple[KnowledgeContext, ...]
```

每个 Context：

```python
KnowledgeContext(
    context_id="P02",
    content="父块原文……",
    supporting_hits=(hit_c01, hit_c03),
)
```

| 字段 | 含义 |
|---|---|
| snapshot | 原检索结果的路由信息，不重新解析 |
| context_id | Baseline 为 Child ID；Parent-Child 为 Parent ID |
| content | 从 PG 读取的最终正文 |
| supporting_hits | 触发该 Context 的实际命中 Child |

Context 不等于 Evidence。它保留知识线索及来源，不自动获得生产业务决策资格。

## 5. 文件中的类和函数

| 定义 | 作用 |
|---|---|
| ContextResolutionIntegrityError | 表示 Child/Link/Parent 完整性错误，继承 ValueError |
| ContextResolutionResult | 结果模型 |
| resolve_knowledge_contexts() | 对外入口：重验输入、处理空结果、创建 PG 会话 |
| _resolve() | 实际算法：复核 Child、策略分支、Parent 校验与分组 |

阅读顺序建议先看对外入口，再看 `_resolve()` 的公共部分，然后分别看两种策略。

## 6. 对外入口逐步展开

```python
result = DenseChildRetrievalResult.model_validate_json(
    result.model_dump_json()
)
```

把输入转为 JSON 后重新解析，让模型规则重新执行。不能只因为对象类型正确，就假设它没有经历绕过校验的构造或嵌套修改。

接着：

```python
if not result.hits:
    return ContextResolutionResult(snapshot=result.snapshot, contexts=())
```

空命中是正常情况，不需要查数据库。

非空时：

```python
sessions = session_factory or get_session_factory()
async with sessions() as session:
    contexts = await _resolve(session, result)
return ContextResolutionResult(snapshot=result.snapshot, contexts=contexts)
```

session_factory 可由调用方注入，用于测试或明确选择数据库环境。`async with` 管理 Session 生命周期；代码只读，没有显式提交写事务。

保持的是同一份路由值，不应理解为 Python 对象引用必须保持 `is` 相等，因为输入重建模型时可以产生新对象。

## 7. 公共准备：提取 Child Hit 和 ID

```python
hits = [item.hit for item in result.hits]
ids = [hit.source.chunk_id for hit in hits]
```

这是两次列表推导。

```text
result.hits：含 hit 与 score 的包装对象
    ↓ 提取 item.hit
hits：KnowledgeHit 列表
    ↓ 提取 source.chunk_id
ids：Child ID 列表
```

例如：

```python
ids = ["C01", "C08", "C03", "C11"]
```

此顺序必须是原始 Child rank 顺序，后续 Parent 排序依赖它。

## 8. 唯一 ID 与连续排名校验

```python
if len(set(ids)) != len(ids) or [hit.rank for hit in hits] != list(range(1, len(hits) + 1)):
    raise ContextResolutionIntegrityError(...)
```

第一部分：

```python
len(set(ids)) != len(ids)
```

set 会去重，如果数量变少就说明出现重复 Child。

第二部分：

```python
list(range(1, len(hits) + 1))
```

四条命中得到 `[1, 2, 3, 4]`。要求实际 rank 与该列表完全一致。

设命中序列为 $H=(h_1,\ldots,h_n)$：

$$
|\{id(h_i)\mid1\leq i\leq n\}|=n
$$

$$
\forall i\in\{1,\ldots,n\},\quad rank(h_i)=i
$$

`[1, 3, 4]`、`[2, 1, 3]` 都不合法。本函数不重新排序后假装输入正确。

## 9. 为什么按 snapshot.profile 查 PG

```python
profile = result.snapshot.index_profile.value
```

IndexProfile 是枚举，`.value` 取数据库保存的策略字符串，例如 `parent-child-v1`。

Child 查询逻辑：

```sql
SELECT ...
FROM knowledge_chunks
WHERE index_profile = :profile
  AND chunk_id IN (...);
```

同一个请求只使用输入快照的 profile。没有重新查询当前 active route，也没有从另一个 profile 借 Child 来补齐。

## 10. SQLAlchemy 查询语法拆解

原代码可展开成：

```python
statement = select(KnowledgeChunkRow).where(
    KnowledgeChunkRow.index_profile == profile,
    KnowledgeChunkRow.chunk_id.in_(ids),
).execution_options(populate_existing=True)

query_result = await session.execute(statement)
rows = list(query_result.scalars())
```

| 写法 | 含义 |
|---|---|
| select(Model) | 查询该 ORM 模型 |
| where(a, b) | 两个条件同时成立，即 AND |
| column.in_(ids) | SQL 的 IN 条件，末尾下划线避免与 Python 关键字混淆 |
| await session.execute() | 异步执行查询 |
| scalars() | 提取结果中的 ORM 对象，而非外层 Row 包装 |
| list(...) | 将结果迭代器收集成列表 |
| populate_existing=True | 用查询结果刷新会话中已有对象的属性 |

最后一项不等于创建跨多次查询的固定快照，也不代表对 Child 加了行锁。

## 11. 字典索引与 Child 完整性

```python
children = {row.chunk_id: row for row in rows}
```

得到：

```python
{
    "C01": child_row_01,
    "C03": child_row_03,
    "C08": child_row_08,
    "C11": child_row_11,
}
```

SQL 查询顺序不需要与 hits 一致，因为后续直接按 ID 查字典。

```python
if set(children) != set(ids):
    raise ContextResolutionIntegrityError(...)
```

`set(children)` 取字典键。要求 PG 找到的 ID 集合与命中 ID 集合相等，缺一个就失败。

## 12. 逐条复核 Hit 与 PG Child

```python
child = children[hit.source.chunk_id]
```

通过来源 Chunk ID 找到权威 Child。

先比较身份元组：

```python
(hit.source.document_id, hit.source.document_version)
!=
(child.document_id, child.document_version)
```

再比较：

```text
document_type
model_id
title
section_path
content
```

使用：

```python
any(getattr(hit, key) != getattr(child, key) for key in fields)
```

`getattr` 根据字符串字段名读属性，`any` 发现任意不同就返回 True。

这一层不比较 Hit 的 source_uri/页码，因为 KnowledgeHit 契约没有这些字段；PG Child 自身的 source_uri 和页码随后会进入 Child 契约校验及 Parent 来源检查。

## 13. `model_fields` 与 `model_validate`

```python
KnowledgeChunk.model_validate({
    key: getattr(child, key)
    for key in KnowledgeChunk.model_fields
})
```

`model_fields` 是 Pydantic 模型声明的字段集合。代码按这些字段从 ORM 行取数据，构成字典，再执行 Child 契约校验。

这里没有把校验后对象赋给变量，是因为后续仍然使用 ORM 行；本次调用的主要作用是发现非法数据并抛异常。

例如 PG 中页码只有一个端点，或结束页早于开始页，不能跳过检查继续解析。

## 14. Baseline：一条 Child 对应一份 Context

```python
if result.snapshot.index_profile == IndexProfile.BASELINE:
    return tuple(
        KnowledgeContext(
            context_id=hit.source.chunk_id,
            content=children[hit.source.chunk_id].content,
            supporting_hits=(hit,),
        )
        for hit in hits
    )
```

等价展开：

```python
contexts = []
for hit in hits:
    child = children[hit.source.chunk_id]
    context = KnowledgeContext(
        context_id=child.chunk_id,
        content=child.content,
        supporting_hits=(hit,),
    )
    contexts.append(context)
return tuple(contexts)
```

输入 $n$ 个 Child，则输出 $n$ 个 Context，顺序不变：

$$
Context_i=(id(h_i),content_{PG}(h_i),(h_i))
$$

公式中的最后一项表示仅包含该 Hit 的支持序列。

正文来自 PG Child，不使用未经校验的其他载荷。Baseline 到这里立即返回，不查询 Link 或 Parent。

### 14.1 单元素元组为什么要逗号

```python
(hit)   # 仍是 hit 本身
(hit,)  # 一个元素的 tuple
()      # 空 tuple
```

逗号才是单元素元组的重要语法标记。

## 15. Parent-Child：查询 Link

针对所有命中 Child，按同一 profile 查询 KnowledgeContextLinkRow。

```python
by_child = {link.chunk_id: link for link in links}
```

此字典不是 Parent 表，而是：

```text
Child ID → Link 行
```

例如：

| chunk_id | context_id | child_ordinal |
|---|---|---:|
| C01 | P02 | 0 |
| C08 | P01 | 2 |
| C03 | P02 | 3 |
| C11 | P03 | 1 |

含义：C03 属于 P02，并且是 P02 构建时父内序号为 3 的 Child。

## 16. 为什么数量和集合都要检查

```python
if len(links) != len(ids) or set(by_child) != set(ids):
    raise ContextResolutionIntegrityError(...)
```

目标是每个命中 Child 恰好一条 Link。

只比数量：预期 C01、C02，但返回两条 C01，数量相同仍然错误。

只比字典键集合：预期 C01、C02，但返回 C01、C01、C02，字典构造可能覆盖重复 C01，键集合仍然相同。

因此需要同时检查查询行数和 ID 覆盖集合。数据库约束通常已限制这些问题，应用检查提供额外的完整性边界。

## 17. Parent ID 集合与 Parent 查询

```python
context_ids = {link.context_id for link in links}
```

这是集合推导式，自动去重：

```text
P02、P01、P02、P03 → {P01, P02, P03}
```

集合不用于决定输出顺序，只用于一次性查询所需 Parent。

随后：

```python
by_parent = {parent.context_id: parent for parent in parents}
```

要求所有被引用 Parent 都存在于同一个 profile：

$$
Keys(by\_parent)=\{parentID(link)\mid link\in links\}
$$

缺失则整次失败。每个 Parent 还要通过 KnowledgeContextUnit 的数据契约校验，包括页码端点规则。

Parent 正文直接读取其 PG content，不从 Child 拼接。只命中一个 Child 时，也可以展开完整 Parent，这是当前策略，不采用命中比例阈值。

## 18. 三张字典如何配合

```python
child = children[hit.source.chunk_id]
link = by_child[hit.source.chunk_id]
parent = by_parent[link.context_id]
```

逐步执行：

```text
hit.source.chunk_id = C03
    ↓ children[C03]
Child C03
    ↓ by_child[C03]
Link：C03 → P02，ordinal=3
    ↓ by_parent[P02]
Parent P02
```

| 字典 | 键 | 值 |
|---|---|---|
| children | chunk_id | PG Child 行 |
| by_child | chunk_id | PG Link 行 |
| by_parent | context_id | PG Parent 行 |

这些字典让遍历每个 Hit 时不再为单个 Child 追加数据库查询，避免常见的逐条查 Parent 的 N+1 访问模式。

## 19. `ordinal_key` 为什么是二元组

```python
ordinal_key = (parent.context_id, link.child_ordinal)
```

它表示“某个 Parent 内的某个 Child 位置”。

例如：

```python
("P01", 0)
("P02", 0)
```

两个键不同，因为不同 Parent 都可以有第 0 个 Child。

如果只记录 ordinal=0，就会把不同 Parent 的合法序号误判为冲突。

数学上，位置身份是笛卡尔积中的一对值：

$$
position(c)=(parentID(c),ordinal(c))
$$

## 20. `ordinals` 集合检查什么

```python
ordinals = set()
```

它记录已经见过的父内位置：

```python
if link.child_ordinal < 0 or ordinal_key in ordinals:
    raise ContextResolutionIntegrityError(...)
ordinals.add(ordinal_key)
```

两种错误：负序号；不同命中 Child 占用同一 Parent 的相同序号。

| Child | Parent | ordinal | 结果 |
|---|---|---:|---|
| C01 | P01 | 0 | 首次出现，允许 |
| C02 | P02 | 0 | 不同 Parent，允许 |
| C03 | P01 | 0 | 与 C01 冲突，拒绝 |
| C04 | P03 | -1 | 负序号，拒绝 |

二元组中的字符串和整数可哈希，因此可以放入 set。列表不能直接作为 set 元素。

## 21. 三种顺序一定要分清

| 顺序 | 产生时机 | 用途 |
|---|---|---|
| Child rank | 本次检索 | 决定 Parent 首次出现顺序 |
| child_ordinal | 文档构建时 | 表达 Child 在 Parent 内的位置 |
| SQL 返回顺序 | 本次数据库查询 | 不作为输出排名依据 |

例如 rank=1 的命中可以是 Parent 内 ordinal=5 的 Child。这完全合法：最相关的内容不一定是父块的第一段。

解析只见到检索命中的子集，不要求命中序号连续。父块完整构建时序号可以是 0～7，本次只命中 1、4、7，也是合法结果。

完整构建契约与在线解析的检查范围不同：前者能检查全部兄弟块连续性，后者不能要求没有被检索命中的兄弟块也出现。

## 22. Child 与 Parent 的来源校验

代码比较：

```text
document_id
document_version
document_type
title
section_path
model_id
source_uri
```

数学表达：设字段集合为 $F$：

$$
\forall f\in F,\quad child[f]=parent[f]
$$

这防止跨文档、跨版本、跨章节或来源不同的错连。

这里不比较 content 相等，因为 Child 本来就是 Parent 内的局部搜索块，正文不应要求与 Parent 完全相同。

当前代码也没有执行 `child.content in parent.content` 的子串证明，不能把来源与页码相容扩大解释为已经证明正文逐字符包含关系。

## 23. 页码校验：先检查有没有，再检查范围

原条件：

```python
if (child.page_start is None) != (parent.page_start is None) or (
    child.page_start is not None and not (
        parent.page_start <= child.page_start <= child.page_end <= parent.page_end
    )
):
    raise ContextResolutionIntegrityError(...)
```

可以拆成两个问题。

### 23.1 页码存在性是否相同

```python
(child.page_start is None) != (parent.page_start is None)
```

比较两个布尔值：一方没有页码、另一方有页码时结果为 True，属于错误。

| Child 有页码 | Parent 有页码 | 结果 |
|---|---|---|
| 否 | 否 | 允许 |
| 是 | 是 | 继续检查范围 |
| 是 | 否 | 拒绝 |
| 否 | 是 | 拒绝 |

### 23.2 Child 页码是否落在 Parent 内

设 Parent 范围为 $[p_s,p_e]$，Child 范围为 $[c_s,c_e]$：

$$
p_s\leq c_s\leq c_e\leq p_e
$$

| Parent | Child | 结果 |
|---|---|---|
| 3～5 页 | 4～5 页 | 允许 |
| 3～5 页 | 3～3 页 | 允许 |
| 3～5 页 | 2～4 页 | 拒绝 |
| 3～5 页 | 5～6 页 | 拒绝 |

前面的 Child/Parent 契约校验已经要求每个范围两端同时存在且顺序合理，才能让这里进行安全比较。

Python 链式比较 `a <= b <= c` 等价于相邻条件同时成立，不是先把 `a <= b` 的布尔结果再拿去与 c 比大小。

## 24. `setdefault(...).append(...)` 如何去重 Parent

```python
grouped.setdefault(parent.context_id, []).append(hit)
```

逻辑展开：

```python
if parent.context_id not in grouped:
    grouped[parent.context_id] = []
grouped[parent.context_id].append(hit)
```

`setdefault` 在键不存在时保存默认列表，并返回对应值；键已经存在时，返回原列表，不替换它。

再对返回的列表执行 append，就把当前 Child Hit 添加到该 Parent 的支持列表中。

注意不要改成：

```python
grouped[parent.context_id] = []
grouped[parent.context_id].append(hit)
```

否则每次遇到 Parent 都清空之前的支持来源。

## 25. 完整分组例子：逐轮看变量变化

输入 Child 排名：

| Child rank | Child | Parent | 父内 ordinal |
|---:|---|---|---:|
| 1 | C01 | P02 | 0 |
| 2 | C08 | P01 | 2 |
| 3 | C03 | P02 | 3 |
| 4 | C11 | P03 | 1 |

初始：

```python
grouped = {}
ordinals = set()
```

第一轮 C01：

```python
ordinal_key = ("P02", 0)
ordinals = {("P02", 0)}
grouped = {"P02": [hit_C01]}
```

第二轮 C08：

```python
ordinals = {("P02", 0), ("P01", 2)}
grouped = {
    "P02": [hit_C01],
    "P01": [hit_C08],
}
```

第三轮 C03：

```python
grouped = {
    "P02": [hit_C01, hit_C03],
    "P01": [hit_C08],
}
```

P02 已经存在，因此不重新插入 Parent，不改变它的字典位置。

第四轮 C11：

```python
grouped = {
    "P02": [hit_C01, hit_C03],
    "P01": [hit_C08],
    "P03": [hit_C11],
}
```

Python 字典保留插入顺序，所以最终 Parent 顺序是 P02、P01、P03，而不是 Parent ID 字典序，也不是 SQL 查询顺序。

## 26. Parent 排名的精确定义

对于 Parent $p$，命中它的 Child 集合记为 $H_p$，用于排序的最佳 Child rank：

$$
r^*(p)=\min_{h\in H_p}rank(h)
$$

Parent 按 $r^*(p)$ 升序排列。同一个 Parent 的 supporting_hits 保持原 Child rank 顺序。

上例中：

$$
r^*(P02)=1,\qquad r^*(P01)=2,\qquad r^*(P03)=4
$$

这里是排序语义，KnowledgeContext 本身没有新增 parent_rank 字段。

不执行分数平均、多 Child 加权、命中数量 boost，也不因为 P02 命中两个 Child 就计算一个新 score。

## 27. 最终结果构造

```python
return tuple(
    KnowledgeContext(
        context_id=key,
        content=by_parent[key].content,
        supporting_hits=tuple(support),
    )
    for key, support in grouped.items()
)
```

`grouped.items()` 产生 `(Parent ID, Hit 列表)`，`for key, support` 把二元组解包成两个变量。

上例输出：

```text
Context P02
    content：P02 的完整原文
    supporting_hits：C01(rank=1)、C03(rank=3)

Context P01
    content：P01 的完整原文
    supporting_hits：C08(rank=2)

Context P03
    content：P03 的完整原文
    supporting_hits：C11(rank=4)
```

外层 tuple 固定 Context 序列，内层 tuple 固定支持列表的序列形态。模型 frozen 与 tuple 不是整个嵌套对象树的递归深冻结，KnowledgeHit 本身仍需避免被后续调用方修改。

## 28. 返回数量的关系

设命中 Child 数量为 $n$，不同 Parent 数量为 $m$。

Baseline：

$$
|Contexts|=n
$$

Parent-Child，非空成功结果：

$$
1\leq m\leq n,\qquad |Contexts|=m
$$

全部命中只属于一个 Parent 时只返回一份 Context；这不是漏结果，全部支持 Child 仍保存在 supporting_hits 中。

当没有命中时，两种策略都返回 0 份 Context。

## 29. 为什么 Parent 原文不由 Child 拼接

Child 带有 overlap，且检索往往只命中部分 Child。直接拼接会面临重复内容、缺少未命中段落、顺序与格式失真等问题。

当前做法：

```text
Child 负责定位
Link 负责明确归属
Parent 保存并提供原始连续上下文
```

所以 Parent content 始终直接读取 KnowledgeContextRow.content，不受当前命中数量影响，也不会把 Child overlap 重复带入 Parent 正文。

## 30. 整体伪代码

```python
def resolve(result):
    revalidate_input(result)
    if no_hits(result):
        return original_snapshot_and_empty_contexts()

    hits = extract_hits(result)
    check_unique_ids_and_contiguous_ranks(hits)
    children = fetch_children_for_snapshot_profile(hits)
    validate_hits_against_children(hits, children)

    if profile_is_baseline(result):
        return one_context_per_child(hits, children)

    links = fetch_exactly_one_link_per_hit(hits)
    parents = fetch_all_referenced_parents(links)
    validate_parent_contracts(parents)

    grouped = {}
    seen_positions = set()
    for hit in hits_in_original_rank_order:
        child, link, parent = follow_mapping(hit)
        check_nonnegative_unique_parent_position(link, seen_positions)
        check_matching_provenance(child, parent)
        check_page_containment(child, parent)
        append_hit_under_first_seen_parent(grouped, parent, hit)

    return contexts_from_parent_original_text(grouped)
```

这是流程说明，不是可以直接替换源码的实现。

## 31. fail-closed：为什么一个错误让整次失败

| 异常 | 原因 |
|---|---|
| 重复 Child 或 rank 不连续 | 输入不能代表可靠的原始命中顺序 |
| PG Child 缺失 | 命中没有对应权威来源 |
| Hit 身份/正文与 PG 不一致 | 输入或数据已经偏离权威记录 |
| Link 缺失或重复 | 无法唯一确定 Parent |
| Parent 缺失 | 无法提供权威上下文 |
| ordinal 非法或冲突 | 映射内部位置不一致 |
| Child/Parent 来源不一致 | 可能错误关联其他文档或章节 |
| 页码存在性不同或越界 | 来源范围不相容 |

代码不会在发现错误后 `continue` 跳过该条，也不会返回已积累的部分 grouped。raise 会中断函数并向调用方传播。

错误类继承 ValueError，方便调用方识别完整性问题；这不表示任何错误都一定表现为该类。Pydantic 契约错误、数据库异常也会按原类型传播。

## 32. 快照保持与当前 PG 读取的边界

输入快照是 v1，即使当前路由已经切到 v2，解析仍携带 v1 的 profile、collection 和 route_version。

但函数只查询 PG，不按 collection 从 Milvus 再读，也不根据 route_version 查询一份版本化 PG 历史快照。

正确性依赖已持久化 Child/Parent/Link 在正常应用路径下不可变。本函数的校验可以发现许多破坏，但不是密码学成员证明。

当前 serving acceptance 摘要主要绑定 Child 业务载荷，没有把全部 Parent/Link 纳入摘要。如果越权 SQL 修改 Parent/Link 后仍满足来源与页码规则，本次 snapshot 本身不能独立证明它们与激活时完全一致。这是此前保留的强化边界，本阶段不修改 schema。

同一个 Session 中执行多条查询，也不自动意味着所有读取都在一个显式 REPEATABLE READ 固定快照中。本文件没有申请 profile source 锁或显式设置隔离级别。

## 33. 复杂度与数据库访问

设命中 Child 数为 $n$，不同 Parent 数为 $m$，业务正文总大小为 $L$。

本地字典、集合构建与遍历通常为期望 O(n+m)，正文比较和序列化还要计入 $L$：

$$
T_{local}\approx O(n+m+L)
$$

辅助字典与分组引用数量约 O(n+m)，除此之外还需要保存/读取正文与模型对象。

正常路径主要查询次数：

| 场景 | 本模块中的主要 SELECT |
|---|---:|
| 空命中 | 0 |
| Baseline | 1 次 Child 查询 |
| Parent-Child | 3 次：Child、Link、Parent |

这些是正常 ORM 查询路径，不包括连接建立、事务控制等协议操作。Parent 合并不是逐 Hit 单独查一次数据库。

Parent 展开可能减少 Context 条数但增加正文长度。因此“返回更少 Context”不代表 token 更少，实际代价应通过 benchmark 测量。

## 34. Python 语法集中速查

| 写法 | 解释 |
|---|---|
| async def | 定义协程函数 |
| await | 等待异步结果 |
| async with | 进入/退出异步资源上下文 |
| `*, session_factory=...` | 该参数必须按名称传入 |
| `X \| None` | 类型允许 X 或 None |
| `[expr for item in items]` | 列表推导 |
| `{key: value for item in items}` | 字典推导 |
| `{expr for item in items}` | 集合推导 |
| `dict[key]` | 按键取值，不存在时会 KeyError |
| `set(dict)` | 取字典键集合 |
| `set.add(value)` | 记录一个已出现元素 |
| `(a, b)` | 二元组，可表示复合身份 |
| `(hit,)` | 单元素元组 |
| `tuple(generator)` | 消费生成器，形成最终元组 |
| `any(generator)` | 任意一项成立就为 True |
| `getattr(obj, name)` | 按字符串读取属性 |
| `a is None` | 判断是否是 None 对象 |
| `a != b` | 判断值是否不同 |
| `and` / `or` | 从左到右短路逻辑 |
| `setdefault(key, [])` | 不存在则创建列表，存在则返回旧列表 |
| `.items()` | 按字典插入顺序遍历键值对 |
| raise | 终止当前正常路径并抛出异常 |

## 35. 常见误解纠正

- ordinal 不是本次检索 rank，也不是数据库自增 ID。
- ordinals 只检查已命中的位置，不要求命中所有兄弟块。
- Parent 去重不是去掉重复 Child；Child 在输入时已经要求唯一。
- supporting_hits 保存 Hit，不只是保存一组 ID，因此保留原 rank 与来源。
- 本阶段不按 Parent 平均分排序，不使用 Auto-Merging 命中比例阈值。
- content 来源是 PG Parent 原文，不是把支持 Child 拼接。
- 没有再次搜索不代表没有再次查 PG，Child 会重新被权威校验。
- READY 表示 ingestion 状态，不是 Context 能否被解析的资格。
- snapshot 证明本次路由选择，不自动证明当前 PG 所有行具有历史版本快照。
- 数据结构校验、来源一致性与答案正确性是不同层面的保证。

## 36. 可运行的纯 Python 分组例子

下面只演示父块分组，不调用数据库、不代替完整来源校验：

```python
hits = [
    {"chunk_id": "C01", "rank": 1, "parent_id": "P02", "ordinal": 0},
    {"chunk_id": "C08", "rank": 2, "parent_id": "P01", "ordinal": 2},
    {"chunk_id": "C03", "rank": 3, "parent_id": "P02", "ordinal": 3},
    {"chunk_id": "C11", "rank": 4, "parent_id": "P03", "ordinal": 1},
]

grouped = {}
seen = set()
for hit in hits:
    position = (hit["parent_id"], hit["ordinal"])
    if hit["ordinal"] < 0 or position in seen:
        raise ValueError("父内位置非法或重复")
    seen.add(position)
    grouped.setdefault(hit["parent_id"], []).append(hit)

assert list(grouped) == ["P02", "P01", "P03"]
assert [h["chunk_id"] for h in grouped["P02"]] == ["C01", "C03"]
assert [h["rank"] for h in grouped["P02"]] == [1, 3]
print("分组与顺序检查通过")
```

## 37. 测试与阅读路径

`test_context_resolution.py` 覆盖正常 Baseline/Parent-Child 解析、同 Parent 合并与顺序、缺失/损坏来源、缺 Link/Parent、页码异常、重复 Child、排名异常、空结果等场景。

单元测试不能代替真实数据库集成与召回效果评测。本文新增文档时不把历史通过数当成本次重跑结果。

建议带着一个例子阅读：先确定 Child 命中顺序，再写出三张字典，再逐轮更新 ordinals 与 grouped，最后检查 Context 输出。

## 38. 源码与配套文档

- [Context Resolution 实现](../backend/src/risk_platform/knowledge/context_resolution.py)
- [Context Resolution 测试](../backend/tests/knowledge/test_context_resolution.py)
- [Retrieval 实现](../backend/src/risk_platform/knowledge/retrieval.py)
- [Serving 契约](../backend/src/risk_platform/knowledge/serving_contracts.py)
- [构建期 Parent/Link 契约](../backend/src/risk_platform/knowledge/indexing/contracts.py)
- [数据库表定义](../backend/src/risk_platform/knowledge/models.py)
- [Embedding 与 Retrieval 指南](knowledge-embedding-retrieval-guide.md)
- [Parent-Child 构建算法指南](knowledge-parent-child-guide.md)
- [数据库持久化指南](knowledge-persistence-guide.md)

## 39. 一页记忆框架

```text
公共部分：
重验输入 → 唯一 Child ID/连续 rank → PG Child → 逐字段复核

Baseline：
每个 Child → 一份 Child Context

Parent-Child：
Child → 恰好一条 Link → Parent 存在
      → 父内位置合法 → 来源一致 → 页码包含
      → 按 Child rank 遍历分组 → Parent 原文 + supporting_hits

顺序规则：
Parent 顺序 = 首次命中该 Parent 的 Child 顺序
支持列表顺序 = 原 Child rank 顺序

失败规则：
任意一条完整性错误 → 整次失败，不返回部分结果
```
