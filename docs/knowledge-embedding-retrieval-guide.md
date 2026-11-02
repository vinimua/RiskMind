# Knowledge RAG：Embedding 与 Dense Child Retrieval 实现指南

本文按 RiskMind 当前源码整理，覆盖实现流程、数据契约、数学含义、校验机制、错误处理与工程边界。目的是既能顺着代码阅读，也能在排查问题和后续评测时作为参考。

文中的示例 ID、集合名称和向量数值仅用于解释，不代表真实评测结果。公式使用 `$...$` 与 `$$...$$`，需使用支持 LaTeX 数学公式的 Markdown 阅读器。

## 1. 模块范围与完整位置

```text
离线/入库侧：
文档 → 构建 Child → PostgreSQL → Child 正文 Embedding → Milvus
                                                      ↓
                                               Rebuild / Validate / Activate

在线检索侧：
检索请求 + index_profile
    ↓
一次性解析 serving route
    ↓
问题 Embedding
    ↓
指定物理 collection 的 Dense Search
    ↓
PG 权威 Child 校验
    ↓
DenseChildRetrievalResult
    ↓ 独立的 context_resolution.py
KnowledgeContext[]
    ↓ 后续生成流程
答案
```

本指南重点是 `embeddings.py` 与 `retrieval.py`，并解释它们依赖的 contracts、Milvus 搜索和 serving route。上下文解析只介绍交接边界，不把 Parent 解析或生成算法算作 Dense Child Retrieval 的实现。

## 2. 核心原则

1. PostgreSQL 是权威存储，Milvus 是可重建的搜索副本。
2. 搜索 profile 必须明确，Baseline 与 Parent-Child 的物理集合隔离。
3. 每次请求只解析一次路由，后续携带同一份 snapshot。
4. Milvus 返回候选，PG 决定候选的身份与业务载荷是否可信。
5. 任一候选完整性异常，整次请求失败，不返回部分成功。
6. 执行成功且没有命中是正常结果；依赖失败不能伪装成空命中。
7. 检索命中是知识线索，不自动成为正式业务 Evidence。

## 3. 文件职责对照

| 文件 | 职责 |
|---|---|
| `embeddings.py` | 请求外部向量模型，校验数量、下标和维度，恢复输入顺序 |
| `retrieval.py` | 固定路由，执行问题向量搜索，校验候选并返回 Child Hit |
| `contracts.py` | 请求、筛选条件、Child Hit、来源引用等基础契约 |
| `serving_contracts.py` | 路由快照、验收记录、最终 Context 契约 |
| `serving.py` | 集合验收、显式激活、解析当前 serving route |
| `milvus.py` | 集合归属、向量搜索、过滤表达式、同步客户端线程桥接 |
| `repository.py` / `models.py` | 权威 Child 的持久化与数据表定义 |
| `ingestion.py` | 把已持久化 Child 正文转成向量并写入 Milvus |
| `context_resolution.py` | Child → Child Context，或 Child → Link → Parent Context |

## 4. Embedding 的数学含义

嵌入模型把文本映射到向量空间：

$$
E:T\rightarrow\mathbb{R}^{d}
$$

其中 $T$ 是文本集合，当前约定 $d=1024$。

对于 Child 正文 $c_i$ 和问题 $q$：

$$
v_i=E(c_i),\qquad v_q=E(q)
$$

Milvus 利用这些向量的相似性寻找候选。向量中的单个分量不能直接解释为“风险程度”“正确率”等业务量。

相同维度并不足以保证兼容：两种不同模型即使都输出 1024 维，向量空间也未必可直接混用。当前默认入库和查询共用 `BAAI/bge-m3`，但没有在每条向量中保存并验证一份完整的 Embedding 模型指纹。

## 5. `embeddings.py` 的全部组成

| 对象 | 作用 |
|---|---|
| `DEFAULT_SILICONFLOW_BASE_URL` | 默认 OpenAI-compatible API 地址 |
| `EMBEDDING_MODEL` | 默认模型 `BAAI/bge-m3` |
| `EMBEDDING_DIMENSION` | 固定约定 1024 维 |
| `EmbeddingClient.__init__()` | 保存 SDK 客户端与模型名，不发送请求 |
| `EmbeddingClient.embed_texts()` | 批量转向量并检查响应 |
| `EmbeddingClient.embed_query()` | 单问题转向量，复用批量校验 |
| `_CLIENT` | 当前进程中的默认客户端缓存 |
| `get_embedding_client()` | 首次读取配置创建客户端，后续复用 |

### 5.1 为什么使用 `AsyncOpenAI`

它是 SDK 客户端类型，能够访问 OpenAI 兼容协议的服务。当前 base URL 指向 SiliconFlow，因此使用该类不代表请求发往 OpenAI 官网。

这里没有本地加载 BGE-M3 权重，也不需要这段 Python 自己完成神经网络计算。模型运行于外部服务，本模块处理协议与数据。

### 5.2 配置读取

| 配置 | 当前行为 |
|---|---|
| `SILICONFLOW_API_KEY` | 从当前进程环境读取；缺失时明确报错 |
| `SILICONFLOW_BASE_URL` | 可覆盖默认接口地址 |
| 默认模型 | 代码常量 `BAAI/bge-m3` |
| 默认维度 | 代码常量 1024，与 Milvus 约定一致 |

本函数不会自行加载 `.env` 或 `.env.example`。模板文件中写有配置名，不等于进程已经获得配置。

客户端创建后再修改环境变量，不会自动改变已有对象中的 key、URL 或模型。正常运行中应通过受控进程生命周期应用配置变更。

## 6. `embed_texts()` 的输入输出

```python
texts = [
    "模型性能下降",
    "标签观察窗口不足",
]

vectors = await client.embed_texts(texts)
```

返回：

```python
[
    [0.1, -0.2, ...],  # 对应 texts[0]，1024 维
    [0.3,  0.4, ...],  # 对应 texts[1]，1024 维
]
```

类型 `list[list[float]]` 表示外层是一批结果，内层是一条向量。

对 $n$ 条输入，目标契约为：

$$
|V|=|T|=n,\qquad \forall i\in\{0,\ldots,n-1\},\ |V_i|=1024
$$

还必须保持 $V_i$ 对应 $T_i$，不能仅保证数量相同。

## 7. `embed_texts()` 的逐步算法

### 7.1 空批次

```python
if not texts:
    return []
```

不调用上游，直接返回空结果。空批次与批次中包含空文本不同。

### 7.2 拒绝空白正文

```python
if any(not text.strip() for text in texts):
    raise ValueError(...)
```

`any()` 表示有任意一条不合法就拒绝整批。`strip()` 仅用于检查，没有把清理后的内容重新赋值，因此实际原文中的空白不被擅自改写。

### 7.3 调用上游

```python
response = await self.client.embeddings.create(
    model=self.model,
    input=texts,
    encoding_format="float",
)
```

这是一次批量请求，不是本函数为每段文本逐个发请求。`await` 让当前协程等待网络响应；它不意味着自动并行执行后面的代码。

`encoding_format="float"` 明确请求浮点数数组格式。本模块没有自行归一化向量。

### 7.4 检查响应数量

```python
if len(response.data) != len(texts):
    raise ValueError(...)
```

数量多或少都失败，不截断、不补齐、不返回部分成功结果。

### 7.5 根据 index 建立对照表

```python
vectors_by_index: dict[int, list[float]] = {}
```

每个响应条目包含 `index` 与 `embedding`，字典保存：

```text
输入位置 → 对应向量
```

取下标：

```python
index = getattr(item, "index", None)
```

缺少属性时得到 None，随后统一拒绝。

对每个下标要求：

$$
index\in\mathbb{Z},\qquad 0\leq index<n
$$

代码还专门拒绝 bool，因为 Python 中 `isinstance(True, int)` 为 True。

使用 `or` 短路判断：例如 index 为 None 时，第一个非法条件已经成立，不会继续执行 `index < 0`。

### 7.6 检查重复、覆盖范围并恢复顺序

```python
if index in vectors_by_index:
    raise ValueError(...)
vectors_by_index[index] = item.embedding
```

这里 `in` 检查字典键，防止同一个输入位置被两条结果覆盖。

输入三条文本，预期下标集合必须是 `{0, 1, 2}`：

$$
Keys(vectors\_by\_index)=\{0,1,\ldots,n-1\}
$$

之后按输入下标顺序取结果：

```python
vectors = [vectors_by_index[index] for index in range(len(texts))]
```

即使响应按 2、0、1 排列，输出仍恢复为 0、1、2。不能把响应排列顺序未经检查地当作输入顺序。

### 7.7 维度检查

```python
if any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
    raise ValueError(...)
```

每条向量都要符合 1024 维约定。

当前 EmbeddingClient 不逐分量检查数值类型、NaN/Inf 或全零，也不验证语义质量。问题检索入口会增加数值检查；不能把这两层检查混为一谈。

## 8. `embed_query()` 为什么只有一行

```python
return (await self.embed_texts([query]))[0]
```

过程为：单个字符串 → 单元素批次 → 复用全部批量校验 → 取唯一向量。

```text
输入："为什么模型性能下降？"
包装：["为什么模型性能下降？"]
批量结果：[[0.1, 0.2, ...]]
返回：[0.1, 0.2, ...]
```

因此文档与问题的调用入口不同，但默认使用同一模型和同一套响应规则。

## 9. 单例、连接池与测试注入

`get_embedding_client()` 使用模块变量 `_CLIENT` 缓存客户端包装对象，复用其 HTTP 连接池。

缓存的不是 Embedding 结果，所以相同文本重复调用仍可能产生新的网络请求与费用。本模块没有文本向量缓存，也没有保证外部调用恰好一次。

`EmbeddingClient(client=..., model=...)` 允许直接注入假 SDK 客户端；检索入口也允许注入 embedding_client、milvus_client 和 session_factory。这是普通参数注入，不依赖通用 DI 框架。

模块本身没有显式设置专用 timeout/max_retries 参数，相关行为取决于使用的 SDK 配置；不能据此宣称“完全没有重试”，也不能宣称实现了项目自己的重试策略。测试中的请求异常会向上传播。

## 10. 入库时到底嵌入了什么

当前 `ingestion.py` 调用：

```python
vectors = await embedder.embed_texts([row.content for row in chunks])
```

只输入 Child 正文，不拼接 title、section_path 或 Parent 正文。标题、章节、来源等字段仍作为 Milvus 业务载荷保存。

Baseline 与 Parent-Child 在这里共用正文向量化逻辑；区别在于上游如何构建 Child，以及后续如何解析 Context。

`zip(chunks, vectors, strict=True)` 保证输入行与向量数量一致。顺序正确依赖 embed_texts 的 index 重排契约。

## 11. 检索请求契约

```python
KnowledgeSearchRequest(
    query="模型性能为什么下降？",
    top_k=5,
    filters=KnowledgeFilters(model_id="M01"),
)
```

| 字段 | 规则与含义 |
|---|---|
| query | 非空白字符串，实际问题文本 |
| top_k | 正整数语义的最多返回数量；当前 Field(gt=0)，没有 strict=True 或上限 |
| filters | 默认生成独立的空筛选对象 |

当前请求模型没有冻结，类型注解和 Field 也不代表所有输入都按严格类型拒绝。检索入口重新序列化校验，是为了让最终调用参数重新经过契约规则。

### 11.1 筛选条件从哪里来

由调用方创建请求时提供，不由 Dense Retrieval 自动从自然语言推断。

当前 benchmark 路径：

```text
评测 JSON
    ↓ BenchmarkDataset 解析
question.request
    ↓ run_benchmark()
retrieve_dense_children(question.request, index_profile=profile)
    ↓
milvus.dense_search(filters=request.filters)
```

没有填写 filters，就使用默认空条件，不额外限制支持的筛选字段。

### 11.2 支持的过滤字段

| 字段 | 空值规则 |
|---|---|
| document_types | 空列表表示不限制；非空则属于其中任一类型 |
| document_id | None 表示不限制；否则精确相等 |
| model_id | None 表示不限制；否则精确相等 |
| document_version | None 表示不限制；否则精确相等 |

不同字段之间是 AND。model_id 是业务风控模型 ID，不是 Embedding 模型名称。

过滤只是调用方指定的搜索范围，不自动构成租户鉴权或数据访问授权。

## 12. 请求二次校验与 profile 校验

```python
request = KnowledgeSearchRequest.model_validate_json(request.model_dump_json())
profile = IndexProfile(index_profile)
```

第一行先转换为 JSON，再重新解析为模型，使字段和业务规则重新执行。原因是已有模型可能来自绕过校验的构造、未经校验的 copy 更新，或可变内部字段修改。

直接对已有模型执行 model_validate，是否重新检查实例取决于 Pydantic 配置；JSON 路径让这里重新走解析流程。

第二行确认策略属于当前固定枚举。profile 是独立参数，不是 request.filters 的字段。

## 13. 路由快照：搜索位置只确定一次

```python
async with sessions() as session:
    snapshot = await resolve_serving_route(session, profile)
```

快照包含：

```text
index_profile = parent-child-v1
collection = pc_rebuild_v2
route_version = 2
```

这里只复制路由值，不复制整个数据库，不锁住管理员后续激活，也不创建数据库 MVCC 快照。

```text
请求 A 读取 v2
    ↓
等待 Embedding
    ↓ 同时管理员激活 v3
请求 A 仍搜索 v2
新请求 B 读取新路由，搜索 v3
```

不重新解析、不回退旧 alias。没有 route 时明确失败，并且发生在 Embedding 请求之前，避免无意义的外部调用。

快照本身也不保证旧集合不会被外部删除；如果集合丢失，检索会失败。旧集合的保留需要运维生命周期配合。

## 14. 两次 PG 会话为何分开

```text
会话 1：读取 route → 关闭
    ↓
Embedding 网络请求
    ↓
Milvus 搜索
    ↓
会话 2：批量查询权威 Child → 校验 → 关闭
```

代码不会在 Embedding 网络等待期间一直保留第一份会话，也没有让 route 查询和 Child 查询处于同一长事务中。

路由一致性靠传递 snapshot，业务载荷稳定性依赖 canonical Child 不可变约束与 PG 实际校验。这不是一个覆盖 PG、Embedding API、Milvus 的分布式事务。

## 15. 问题向量的额外校验

查询入口要求：

$$
v_q\in\mathbb{R}^{1024},\qquad \forall j,\ v_{q,j}\text{ 有限},\qquad v_q\neq\mathbf{0}
$$

对应代码拒绝：

- 维度不等于 1024。
- 分量是 bool 或不是 Real。
- NaN 或正负无穷。
- 全零向量。

`not any(vector)` 用于检测全零，不要求每个分量都非零。包含一些 0 的正常向量仍然合法。

这层检查也适用于注入的自定义 EmbeddingClient，避免完全依赖默认实现。它没有执行单位长度归一化，也不能通过有限性与维度证明向量语义正确。

## 16. Milvus 搜索前的目标检查

内部同步 `search()` 依次执行：

1. 获取注入或默认 Milvus 客户端。
2. 检查 snapshot.collection 是否真实存在。
3. 检查物理集合名称及 profile description marker 兼容。
4. 调用 dense_search，显式传 snapshot.collection。

集合不存在就失败，不会像入库路径那样创建一个空集合。检索是读操作，不应自动修复缺失的 serving 构建。

marker 形如 `riskmind:index-profile:parent-child-v1`。Baseline 仍有无 marker 历史集合的兼容规则，这是既有迁移边界。

底层 dense_search 虽然保留了旧 alias 默认参数，但本入口总是显式传物理 collection，不能据此误认为线上请求会 fallback 到 alias。

## 17. Dense Search 的数学目标和实际参数

对非零问题向量 $q$ 与 Child 向量 $x$，余弦相似度：

$$
cos(q,x)=\frac{\sum_{j=1}^{d}q_jx_j}{\sqrt{\sum_{j=1}^{d}q_j^2}\sqrt{\sum_{j=1}^{d}x_j^2}}
$$

理论范围：

$$
-1\leq cos(q,x)\leq1
$$

分数越大，方向越相近。分数不是正确率，不应把 0.8 解读为 80% 正确。

数学上的目标是从符合过滤条件的 Child 集合 $C_F$ 中选取高相似度候选：

$$
TopK_{c\in C_F}\ cos(v_q,v_c)
$$

实际使用 Milvus 索引搜索，不据此保证穷举精确 Top-K 或 100% 召回。

| 参数 | 当前值/含义 |
|---|---|
| data | `[vector]`，一次只搜索一个问题向量 |
| anns_field | `dense_vector` |
| metric_type | `COSINE` |
| limit | request.top_k |
| ef | `max(64, top_k)` |
| output_fields | Child 来源与正文业务字段 |
| filter | 根据 request.filters 构造的表达式 |

ef 是搜索参数，不是返回数量，也不代表当前实现了并发请求数限制。

Milvus SDK 响应字段名叫 `distance`；本项目在 COSINE 搜索语义下将它转成 `score`。不要因为名字是 distance 就按“越小越好”处理。

当前 dense_search 调用没有显式传 consistency_level；不能把 serving 验收使用的 Strong 读取自动套到这里。读取一致性还受集合/客户端配置影响。

## 18. 过滤表达式如何构造

例如：

```python
KnowledgeFilters(
    document_types=[KnowledgeDocumentType.INCIDENT_REPORT],
    model_id="M01",
    document_version="v2",
)
```

生成的逻辑类似：

```text
document_type in ["INCIDENT_REPORT"]
and model_id == "M01"
and document_version == "v2"
```

字段名来自固定代码，值用 json.dumps 编码；引号、反斜杠等字符按 JSON 字符串方式转义，不直接拼入未经编码的值。这不是 SQL 查询，也不是允许调用方传入任意 Milvus 表达式。

没有任何条件时得到空表达式。过滤先限定搜索范围，PG 还会在结果返回后再次核对实际 Child 是否符合条件。

## 19. 同步 SDK 与异步函数的桥接

```python
candidates = await milvus.acall(search)
```

底层 `acall` 使用事件循环的 run_in_executor，把同步 Milvus 调用交给专用 ThreadPoolExecutor，当前 max_workers=1。

```text
事件循环：提交 search → await → 可调度其他协程
专用线程：has_collection → owner 检查 → search → 返回
```

每进程一个专用线程，不意味着整个部署只有一个请求；并发量较大时也可能在该线程队列中等待。该封装不会自动把所有服务实例串行成一个全局线程。

## 20. Milvus 候选格式

底层把 SDK 返回值转换为字典列表：

```python
[
    {
        "chunk_id": "C08",
        "document_id": "DOC01",
        "document_version": "v1",
        "document_type": "INCIDENT_REPORT",
        "title": "模型异常调查",
        "section_path": "根因分析",
        "content": "标签观察窗口不足……",
        "model_id": "M01",
        "source_uri": "example://DOC01",
        "page_start": 3,
        "page_end": 3,
        "score": 0.91,
    }
]
```

这些是待校验候选，不会因为来自 Milvus 就直接成为可信 Hit。

## 21. `_authoritative_hits()` 的校验流水线

### 21.1 数量、ID、零命中

要求：

$$
0\leq |Candidates|\leq top\_k
$$

每个 chunk_id 必须是非空字符串，且没有重复。零候选直接返回空元组，不查 PG。

### 21.2 按 profile 和 ID 批量查 PG

```sql
SELECT ...
FROM knowledge_chunks
WHERE index_profile = :snapshot_profile
  AND chunk_id IN (...);
```

查询不依赖 ingestion_status，不要求 READY。REBUILD 可以从仍为 PENDING 的权威 Child 构建并验收，因而 serving retrieval 不能错误依赖 ingestion 的状态。

`populate_existing=True` 刷新会话缓存对象的属性，不代表给整个流程建立固定数据库快照。

### 21.3 确认 ID 集合完全匹配

```python
by_id = {row.chunk_id: row for row in rows}
if set(by_id) != set(ids):
    raise ChildRetrievalIntegrityError(...)
```

Milvus 命中在 PG 对应 profile 中不存在时，整次失败。不能查询其他 profile 代替，也不能只忽略缺失候选。

字典解决的是按 ID 对应；SQL 的 IN 查询没有承诺按候选列表顺序返回。

### 21.4 检查分数与降序

每条 score 必须为有限 Real，不能是 bool。按候选原序检查：

$$
s_1\geq s_2\geq\cdots\geq s_m
$$

`previous_score = math.inf` 使第一条合法有限分数可以通过。相同分数允许，保留原序，不增加 ID 排序作为 tie-break。

当前代码没有检查 COSINE 理论域 `[-1, 1]`，因此有限但超范围的分数仍可能通过。这是已记录边界，不应在解释中宣称已实现。

### 21.5 逐字段比较业务载荷

从 PG 构造 payload，对 Milvus 候选逐字段确认存在且相等：

| 字段 | 保护目的 |
|---|---|
| chunk_id | 搜索对象身份 |
| document_id、document_version | 文档与不可变版本 |
| document_type | 文档分类 |
| title、section_path | 阅读标题及章节定位 |
| content | 实际 Child 正文 |
| model_id | 业务模型归属 |
| source_uri | 原始来源地址 |
| page_start、page_end | 页码范围 |

设 $P$ 为这些字段的投影函数：

$$
\forall c\in Candidates,\quad P(c_{Milvus})=P(c_{PG})
$$

缺字段也失败，不只是值不同才失败。额外候选字段不是这一比较的对象。

不会重新计算或比较 dense_vector，也不比较 PG 状态与时间字段。因此这不是对向量模型结果的语义真实性证明。

### 21.6 Child 契约与过滤复核

```python
child = KnowledgeChunk.model_validate(payload)
```

校验 PG 业务载荷满足 Child 数据契约，再执行 `_matches_filters(child, request)`。

文档类型列表内是 OR，不同字段之间是 AND：

$$
Match(c,F)=TypeMatch(c,F)\land\bigwedge_{k\in K}(F_k=None\lor c_k=F_k)
$$

其中 $K=\{document\_id,model\_id,document\_version\}$。

PG 校验保证返回结果没有越过这些过滤条件，但不证明 Milvus 已召回所有应当召回的 Child。

## 22. 输出契约与排名来源

```text
DenseChildRetrievalResult
├── snapshot
│   ├── index_profile
│   ├── collection
│   └── route_version
└── hits: tuple[RetrievedKnowledgeHit, ...]
    ├── hit: KnowledgeHit
    │   ├── source: document_id / document_version / chunk_id
    │   ├── document_type / model_id
    │   ├── title / section_path / content
    │   └── rank
    └── score
```

rank 由 `enumerate(candidates, 1)` 生成，按 Milvus 候选原序为 1 到 N。PG 查询顺序不影响它。

正文与来源基于 PG 构造；score 与排名来自 Milvus 搜索。不把 Parent 内容放入 Child Hit，也不在这里执行 Parent 去重。

输出 KnowledgeHit 不直接携带 source_uri 或页码，虽然这些字段参与了 PG/Milvus 比对；来源引用使用文档 ID、版本与 Chunk ID 定位。

两个结果包装模型采用 extra='forbid'、frozen=True，hits 使用 tuple。不过 KnowledgeHit 本身不是冻结模型，不应把外层 frozen 理解为整个对象树绝对不可变。

## 23. fail-closed 与失败矩阵

| 异常情况 | 行为 |
|---|---|
| 请求或 profile 非法 | 搜索前报错 |
| 没有 serving route | Embedding 前失败，不 fallback alias |
| Embedding API 失败 | 异常向上传播，不搜索 |
| 问题向量非法 | 搜索前抛完整性错误 |
| 物理集合缺失或 owner 冲突 | 失败，不创建集合 |
| Milvus 网络失败 | 失败，不返回空成功 |
| 返回超过 top_k、ID 非法或重复 | 整次失败 |
| PG 缺少某候选或 profile 错误 | 整次失败 |
| score 非有限、非法或升序 | 整次失败 |
| 正文/来源字段不一致 | 整次失败 |
| 实际 PG Child 不符合 filters | 整次失败 |
| 正常搜索返回零条 | 返回原 snapshot 与空 hits |

例如三条候选中第二条正文损坏，不会返回第一和第三条作为部分成功，而是整次失败。

这里的完整性指数据来源和格式等一致性，不意味着召回内容一定能回答问题。业务上的拒答、证据资格判定和答案生成在其他阶段处理。

## 24. 完整请求示例

调用方创建请求：

```python
request = KnowledgeSearchRequest(
    query="标签观察窗口不足会造成什么影响？",
    top_k=3,
    filters=KnowledgeFilters(
        document_types=[KnowledgeDocumentType.INCIDENT_REPORT],
        model_id="M01",
    ),
)
```

执行链：

```text
1. 确认请求合法，profile=parent-child-v1
2. 路由解析得到 pc_rebuild_v2，route_version=2
3. query 转成 1024 维非零有限向量
4. 只在 pc_rebuild_v2 中按 filters 搜索
5. Milvus 返回：C08=0.91、C03=0.86、C12=0.86
6. PG 批量查对应 profile 下三个 Child
7. 核对 ID、分数、正文、来源、页码和过滤条件
8. 形成 rank 1、2、3 的可信 Child Hit
9. 返回原 snapshot 与 hits
```

之后如果 C08、C03 同属 P02，context_resolution 才会将它们合并成一份 Parent Context。Dense Retrieval 不因为两个 Child 同属 Parent 就提前减少结果。

## 25. 与线上新增知识的隔离

```text
serving v1 已激活
    ↓
PG 新增文档，完成 staging / ingestion
    ↓
没有重建并激活新版本
    ↓
检索仍然只搜索 serving v1
```

PG 中出现更多 Child，不会让查询自动搜索 ingestion target，也不会让 v1 物理集合自动变成新快照。

PG 校验从当前权威存储读取命中 Child；正常路径依赖既有 Child 不可变。route snapshot 不是覆盖当前 PG 所有表的版本化快照，也不是每条候选都对 acceptance digest 做实时成员证明。

## 26. 与参考项目的对比

参考代码主要位于 `python/app/core/embeddings.py`、`python/app/config.py`、`python/app/kb/dualwrite.py`。

| 方面 | 参考项目 | 当前实现 |
|---|---|---|
| 基本方法 | AsyncOpenAI 调外部 Embedding | 相同 |
| 默认模型 | BAAI/bge-m3 | 相同 |
| 配置 | BaseSettings，读取 .env | 默认客户端直接读进程环境 |
| 默认模型选择 | settings.embed_model | 代码常量 |
| 封装 | 模块函数与缓存 SDK 客户端 | EmbeddingClient + 默认缓存对象 |
| 响应顺序 | 直接按 response.data 提取 | 按 index 校验和重排 |
| 数量/维度校验 | 核心 embed_texts 中不执行 | 每次请求检查 |
| 入库 Embedding 文本 | category + questions + answer | Child content |
| 客户端复用 | 有 | 有 |

参考实现更短，当前增加的主要是数据边界校验，不是新的神经网络算法。

拼接标题/分类可能补充背景，也可能引入噪声；正文单独嵌入更直接，但短 Child 可能缺少章节背景。两种输入谁的召回效果更好，需要固定语料和问题集进行评测，不能从代码长度或校验数量推断。

## 27. 性能、复杂度与成本

Embedding 客户端本地整理的开销约为：

$$
T_{local}=O(n\times d),\qquad Memory=O(n\times d)
$$

这包括已返回向量的数据量，不代表上游模型推理复杂度。Python 对象、响应和重排列表也会占用额外内存；重排通常引用已有向量列表，不应简单当成完整复制全部数值。

一次检索延迟可拆为：

$$
T_{request}\approx T_{route}+T_{embed}+T_{threadqueue}+T_{milvuschecks}+T_{search}+T_{PG}+T_{validation}
$$

候选数为 $k$，业务载荷总长度为 $L$ 时，ID 字典和集合检查通常约 O(k)，逐字段正文比较需要考虑 O(L)。不能给 Milvus ANN 搜索直接套用本地 Python 的 O(k) 结论。

当前每个问题单独调用 embed_query；没有批量问题检索、向量缓存或本文新增的性能优化。实际费用和耗时应测量，重试也可能重复计费。

## 28. 当前明确边界

1. COSINE score 没有执行理论范围 [-1,1] 校验，仅检查有限实数与降序。
2. EmbeddingClient 检查维度和对应关系，但不独立验证上游向量的语义正确性。
3. 入库正文向量没有在 EmbeddingClient 层逐数值检查有限性；检索问题向量有额外检查。
4. profile 并不自动等于完整 Embedding 模型指纹；换模型需要兼容性和重建决策。
5. frozen 不等于递归深冻结，调用方应避免修改嵌套命中对象。
6. profile 过滤不等于用户鉴权或租户隔离机制。
7. 路由快照不是跨 PG/Milvus 的事务快照，依赖不可变来源与已验收物理集合。
8. 没有集成 BM25、Reranker、Query Rewrite 到这条 Dense Child 主链。
9. 没有最低相似度阈值；低分候选也可能在 top_k 中返回，相关性需评测和后续策略判断。
10. 存储一致不等于答案正确；生成侧需要单独的正确性、忠实度与引用质量评价。

## 29. 测试覆盖如何阅读

### 29.1 Embedding 单元测试

`test_embeddings.py` 覆盖：环境配置与复用、默认参数、缺 key、输入顺序恢复、单问题包装、空批次、空白输入、维度错误、数量错误、非法/重复下标与上游异常传播。

### 29.2 Retrieval 单元测试

`test_retrieval.py` 覆盖：

- 两种 profile 的路由与权威来源。
- 不要求 READY，不执行 Parent 解析。
- 缺 route 时不调用 Embedding、不 fallback alias。
- Embedding 等待期间切换 route，本次请求仍用旧 snapshot。
- 远程候选缺失、损坏、重复等导致整次失败。
- 过滤条件在 PG 二次检查。
- 非法问题向量在搜索前失败。
- 新增来源不会自动改变旧 serving snapshot。
- 依赖异常不被伪装成空成功。
- 同分保留原始排名。

真实存储集成场景另见 `test_retrieval_integration.py`，需要相应服务及测试开关。单元测试使用替身，不能当作真实模型效果或真实部署性能结论。

本次文档整理不重新宣称执行上述测试；此前对应注释修改后的测试记录为 Embedding 14 passed、Retrieval 26 passed。

## 30. 建议的排查顺序

| 现象 | 先看哪里 |
|---|---|
| 找不到 API key | 当前进程环境与 get_embedding_client |
| 返回数量/下标错误 | Embedding 响应契约与上游兼容性 |
| 维度不匹配 | 实际上游模型、1024 维约定、集合 schema |
| 没有线上 route | 是否完成 REBUILD → 验收 → 显式激活 |
| 线上集合缺失 | route 指向的物理集合是否仍保留 |
| PG/Milvus 载荷不一致 | 权威数据、目标集合及写入/重建历史 |
| 零命中 | filters、集合数据、路由版本和实际搜索响应 |
| 有结果但不相关 | 语料、切分、Embedding 输入、召回指标 |
| Parent 内容不对 | 后续 context_resolution 的 Link/Parent 归属 |

排查时不要打印密钥或完整授权头；只确认是否配置、实际模型名、维度、路由版本和适当的错误类型。

## 31. Python 与 Pydantic 语法速查

| 语法 | 在本模块中的用途 |
|---|---|
| `async def` / `await` | 异步等待数据库或上游请求 |
| `async with` | 管理会话与资源退出 |
| 参数中的 `*` | 强制后续参数按名称传入 |
| `getattr(obj, key, default)` | 读取动态字段名，或缺失时返回默认值 |
| `any(...)` / `all(...)` | 检查任一非法或全部条件成立 |
| `dict[int, list[float]]` | 下标到向量的映射类型 |
| 字典推导式 | 构造 ID→PG 行或字段→值 |
| `set(...)` | 判断唯一性与 ID 集合是否完整 |
| `enumerate(candidates, 1)` | 生成连续的 1-based rank |
| `Field(allow_inf_nan=False)` | 结果分数拒绝 NaN/Inf |
| `model_validate_json(model_dump_json())` | 经 JSON 重新执行契约校验 |
| `ConfigDict(extra='forbid', frozen=True)` | 拒绝额外字段与字段重新赋值 |

## 32. 源码与配套文档

- [Embedding 实现](../backend/src/risk_platform/knowledge/embeddings.py)
- [Dense Child Retrieval 实现](../backend/src/risk_platform/knowledge/retrieval.py)
- [基础契约](../backend/src/risk_platform/knowledge/contracts.py)
- [Serving 契约](../backend/src/risk_platform/knowledge/serving_contracts.py)
- [Serving 实现](../backend/src/risk_platform/knowledge/serving.py)
- [Milvus 适配](../backend/src/risk_platform/knowledge/milvus.py)
- [上下文解析](../backend/src/risk_platform/knowledge/context_resolution.py)
- [Embedding 测试](../backend/tests/knowledge/test_embeddings.py)
- [Retrieval 测试](../backend/tests/knowledge/test_retrieval.py)
- [真实检索集成测试](../backend/tests/knowledge/test_retrieval_integration.py)
- [参考项目 Embedding](../python/app/core/embeddings.py)
- [入库与重建指南](knowledge-ingestion-guide.md)
- [数据库持久化指南](knowledge-persistence-guide.md)
- [评测运行说明](runbooks/knowledge-evaluation.md)

## 33. 最终记忆框架

```text
Embedding：
文本 → 请求模型 → 查数量 → 查 index → 恢复顺序 → 查维度 → 向量

Retrieval：
校验请求 → 固定路由 → 问题向量 → 指定集合搜索
    → 查 ID → 查 PG → 查 score → 查载荷 → 查 filters → 可信 Child

职责边界：
Milvus 找相似候选；PG 确认身份和业务载荷；Context Resolution 决定最终阅读内容。
```
