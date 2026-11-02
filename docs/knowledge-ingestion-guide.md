# Knowledge Ingestion：增量入库、重建与一致性完整指南

本文按当前 `backend/src/risk_platform/knowledge/ingestion.py` 实现整理，覆盖已经讨论的概念，以及阅读代码时容易忽略的事务、并发、失败恢复和 Python 语法。本文解释现有行为，不引入新的策略或流程。

公式采用 Markdown 常见的 LaTeX 写法：行内公式 `$...$`，独立公式 `$$...$$`；需使用支持数学公式的 Markdown 阅读器。

## 1. 先确定这个文件在做什么

```text
KnowledgeDocument
    ↓ Baseline / Parent-Child 构建策略
PreparedKnowledge
    ↓ repository.stage_prepared()
PostgreSQL：Document / Build / Child / Parent / Link
    ↓ ingestion.py 只读取已保存的 Child
Embedding API
    ↓
Milvus：Child 正文向量 + Child 来源字段
```

`ingestion.py` 的职责是把 PostgreSQL 中已经确定的 Child 物化成 Milvus 搜索数据。

它不负责重新切分文档、构建 Parent、写入 Parent/Link、执行问题检索，也不负责自动激活线上路由。

这里的“物化”可以理解为：把权威数据转换成适合查询的实际存储副本。

## 2. 必须掌握的名词

| 名词 | 当前项目中的含义 |
|---|---|
| PG | PostgreSQL，保存权威知识数据的关系型数据库 |
| authoritative store | 权威存储，发生数据冲突时以这里的业务数据为准 |
| Milvus | 保存向量和来源字段，提供向量搜索的数据库 |
| read model | 为查询准备的副本；本项目可从 PG 中的 Child 重建 |
| Child / Chunk | 用于生成向量和搜索的文本块 |
| Parent | Parent-Child 策略中用于返回完整上下文的父块，保存在 PG |
| collection | Milvus 中的一组数据，近似理解为关系型数据库的一张表 |
| index_profile | 索引策略及其版本，如 `baseline-v1`、`parent-child-v1` |
| INGESTION | 持续增量写入目标的用途 |
| REBUILD | 完整重建候选集合的用途 |
| acceptance | 验收通过后保存的构建验收记录 |
| activation | 显式将线上检索路由切换到已验收集合 |
| serving | 正式为查询提供检索服务 |

一个 collection 可以存储很多篇文档的 Child，不是一篇文档对应一个 collection。Parent-Child 的 Milvus 集合仍然只保存搜索 Child，不保存 Parent 正文。

## 3. profile、collection、purpose 和 route 的区别

例如：

```text
profile = baseline-v1
    ├── baseline_ingest       purpose = INGESTION
    ├── baseline_rebuild_v1   purpose = REBUILD
    └── baseline_rebuild_v2   purpose = REBUILD

active route：baseline-v1 → baseline_rebuild_v1
```

以上集合名称都是示例。

profile 回答“采用哪套索引规则”；collection 回答“数据放在哪个物理集合”；purpose 回答“这个集合用来持续入库还是完整重建”；route 回答“本次线上检索应该搜索哪个集合”。

每个 profile 至多有一个 INGESTION 目标，可以有多个 REBUILD 目标，同一时刻只有一个当前 serving route。INGESTION、REBUILD 都不会因为登记用途就自动成为线上服务集合。

`bind_collection()` 只登记 PG 归属；`activate_serving_collection()` 才负责线上路由切换。

## 4. 文件中的所有函数

| 函数 | 输入重点 | 作用 | 返回 |
|---|---|---|---|
| `ingest_pending()` | collection、profile、batch_size、可选客户端 | 增量写入 PENDING Child，成功后标 READY | 本次实际转为 READY 的行数 |
| `rebuild_collection()` | 同上 | 从全部持久化 Child 重建、验收完整集合 | 本轮写入行数，或已有验收记录的来源数量 |
| `_preflight_collection()` | collection、profile、可选 Milvus 客户端 | 只读检查已有目标的归属兼容性 | 无 |
| `_validate_options()` | collection、batch_size | 检查基础参数 | 无 |
| `_embed_and_upsert()` | 一批 Child 和客户端 | 向量化、构造载荷、ensure/upsert/flush | 无 |
| 内部 `check()` | 闭包捕获预检参数 | 同步执行 Milvus 预检 | 无 |
| 内部 `ensure_empty_collection()` | 闭包捕获重建参数 | 空来源时创建或检查目标 | 无 |
| 内部 `write_batch()` | 闭包捕获本批载荷 | 在同一线程任务中完成写前检查和写入 | 无 |

下划线开头表示模块内部辅助函数的约定，不是 Python 强制禁止外部调用。

## 5. 数据状态：PENDING 与 READY 到底证明什么

设某个 profile 下的全部 Child 为 $S_p$，其中：

$$
P_p=\{c\in S_p\mid status(c)=PENDING\}
$$

$$
R_p=\{c\in S_p\mid status(c)=READY\}
$$

当前状态集合下：

$$
S_p=P_p\cup R_p,\qquad P_p\cap R_p=\varnothing
$$

`PENDING` 表示还没有记录该 Child 完成 ingestion。`READY` 表示 ingestion 写入、flush 成功后，PG 状态更新已提交。

READY 不证明任意 REBUILD 集合已包含这个 Child，也不证明该集合已上线，更不证明知识内容在业务上正确。

反过来，PENDING Child 也可以被完整 rebuild 包含并参与 serving；因为 rebuild 使用权威 Child，不依赖 ingestion 的完成状态。

## 6. 默认参数和依赖注入

```python
DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 64
```

批大小是一次处理多少个 Child，不是字符数，也不是向量维度。Child 的 400/60 切分规则在上游已经执行。

设固定来源有 $N$ 个 Child，批大小为 $B$，在没有失败和并发变化时，非空处理批次数为：

$$
K=\left\lceil\frac{N}{B}\right\rceil
$$

例如 $N=70,B=32$，批次大小是 32、32、6；之后还需要一次查询发现没有更多数据。

客户端参数可不传：

```python
sessions = session_factory or get_session_factory()
embedder = embedding_client or get_embedding_client()
```

调用方可以提供测试会话和假客户端；生产环境使用项目默认客户端。这是普通参数注入，没有引入 Factory/Registry 框架。`or` 按真假值选择，不是专门检查 `None` 的语法。

## 7. `_validate_options()`：入口参数校验

当前依次检查：

1. collection 不能缺失或全为空白。
2. collection 不能等于旧的 `riskmind_knowledge_active` alias。
3. `batch_size` 必须满足代码中的范围比较。

数学上的意图是：

$$
1\leq B\leq64
$$

注意实际实现只有范围比较，不额外执行严格整数校验。类型注解 `int` 不会自动在运行时拒绝布尔值或浮点数，因此不要把文档理解成代码已经执行了所有类型验证。

`collection.strip()` 仅用于判断是否为空，函数没有把去空白后的名称重新赋值。预检及 Milvus 本身还会继续检查目标；这个函数不是完整的 Milvus 命名规则校验器。

## 8. `_preflight_collection()`：为什么先查 Milvus

增量与重建的顺序都是：

```text
参数检查 → Milvus 只读预检 → PG 登记归属 → 正式写入
```

如果先登记 PG，再发现 Milvus 的同名集合属于另一 profile，就可能留下错误绑定，尤其会占用唯一的 INGESTION 目标。

预检规则：

| 目标情况 | 结果 |
|---|---|
| collection 不存在 | 允许，后续可以创建 |
| 存在且显式 profile marker 匹配 | 允许 |
| 存在但显式 profile marker 属于其他策略 | 拒绝 |
| 没有 profile marker，且当前是 Baseline | 按迁移兼容逻辑允许 |
| 没有 profile marker，当前是 Parent-Child | 拒绝 |
| 返回的物理名称与请求名称不一致 | 拒绝 alias 目标 |

显式 marker 形如 `riskmind:index-profile:baseline-v1`，保存在 Milvus collection description 中。

只读预检不创建、不 load、不写 PG；即使待处理队列为空也执行。

它不能消除跨系统竞争：预检时不存在的集合，之后可能被外部程序创建成错误归属。因此正式写入前 `ensure_collection()` 会再次检查。预检通过不等于获得跨 PG/Milvus 的全局占有权。

## 9. `bind_collection()`：PG 归属登记

调用发生在独立 PG 事务中：

```python
async with sessions() as session:
    async with session.begin():
        await bind_collection(...)
```

处理顺序：

```text
验证用途
    ↓
申请 profile 归属锁
    ↓
申请 collection 归属锁
    ↓
同名集合已登记？
    ├── 同 profile、同用途：幂等成功
    └── 不同 profile 或用途：拒绝
    ↓ 尚未登记
如果是 INGESTION，检查该 profile 是否已有其他目标
    ↓
插入归属记录，flush
    ↓
调用方事务提交
```

例如同一个 profile 同时绑定 ingestion_A、ingestion_B，由 profile 锁排队；两个不同 profile 同时绑定 shared_collection，由 collection 锁排队。数据库主键和部分唯一索引提供最后兜底。

这里登记的集合可能尚未在 Milvus 创建。绑定成功不意味着 Embedding 或写入已经成功。

## 10. `ingest_pending()`：完整执行链

```text
① 校验参数、规范 profile
② Milvus 只读预检
③ PG 绑定 INGESTION 目标并提交
④ 读取当前 profile 的一批 PENDING
⑤ 如果为空，返回 completed
⑥ 获得 collection_write_guard
⑦ Embedding → ensure_collection → upsert → flush
⑧ 退出 guard
⑨ 新 PG 事务：把本批仍为 PENDING 的行改为 READY
⑩ 累加实际更新数量，回到④
```

### 10.1 为什么按 `chunk_id` 排序

`list_pending_chunks()` 按 profile 和 PENDING 过滤，按 chunk_id 排序并限制数量。相同静态数据集下，批次选择可预测。

chunk_id 的字符串顺序不是原文段落顺序。这个排序服务于稳定处理，不代表相关度。

### 10.2 为什么增量读取没有游标

每批成功后，已处理行从 PENDING 变成 READY，下一次查询自然跳过它们。失败的行仍为 PENDING，下次调用会重新读取。

### 10.3 为什么返回值不是批次长度之和

`mark_chunks_ready()` 只更新目标 profile 中仍为 PENDING 的指定 ID，并返回实际影响行数。

$$
completed=\sum_{j=1}^{K} updated\_rows_j
$$

并发请求可能已经标记过部分行，因此本次更新数可能小于本批长度。重复调用不会重写 READY 行的时间。

### 10.4 是否一次运行一定处理所有新知识

函数持续查询直到某次看到空队列。运行期间新增的 PENDING 可能被当前调用处理，也可能在返回后留待下一次执行。它没有声明“调用开始时固定批次快照”的语义；持续有新数据时，循环也可能运行很久。

## 11. `collection_write_guard()`：写入保护区

```python
async with collection_write_guard(sessions, collection):
    await _embed_and_upsert(...)
```

其实际结构位于 `serving.py`，可概括为：

```python
@asynccontextmanager
async def collection_write_guard(sessions, collection):
    async with sessions() as session:
        async with session.begin():
            await lock_for_this_collection()
            if collection_has_acceptance():
                raise ServingEligibilityError(...)
            yield
```

进入 `yield` 之前，先拿锁并检查是否已验收；`yield` 期间执行调用方缩进块；退出后结束事务并释放事务级锁。异常也会触发上下文清理。

当前实现把 Embedding 等待也包含在 guard 里，因此保护区不只是 Milvus upsert 那几行。

### 11.1 三类锁不要混淆

| 锁 | 业务键示意 | 职责 |
|---|---|---|
| profile 绑定锁 | `riskmind:knowledge-index:profile:baseline-v1` | 协调同策略集合归属登记 |
| collection 绑定锁 | `riskmind:knowledge-index:collection:collection_A` | 协调同名集合归属登记 |
| collection 物化锁 | `riskmind:knowledge-materialization:collection_A` | 写入和验收互斥，防止已验收目标再写 |

此外，staging 与 serving 验收/激活还有 profile source 的共享/独占锁，用于协调权威来源变化。绑定锁不等于 source 锁。

锁名通过 PostgreSQL `hashtextextended` 转成编号，`pg_advisory_xact_lock` 申请事务级独占 advisory lock。它们保护遵守相同协议的应用路径，不会阻止任意人工 SQL 或外部 Milvus 写入。

### 11.2 为什么 INGESTION 不会被正常验收冻结

serving 验收只允许 REBUILD。INGESTION 持续可写，不能生成 serving acceptance；REBUILD 验收通过后固定为不可变候选快照。

```text
INGESTION：A → B → C → 持续新增

REBUILD v1：完整构建 → 验收 → 冻结 → 激活
REBUILD v2：完整构建 → 验收 → 冻结 → 后续激活
```

## 12. `_embed_and_upsert()`：一批数据如何变成向量

### 12.1 只嵌入 Child 正文

```python
vectors = await embedder.embed_texts([row.content for row in chunks])
```

设本批 Child 为 $c_1,\ldots,c_B$，嵌入函数为 $E$：

$$
v_i=E(content(c_i)),\qquad v_i\in\mathbb{R}^{1024}
$$

当前没有把 title、section_path 或 Parent 正文拼入 Embedding 输入，但这些来源字段仍会保存到 Milvus 载荷里。

默认上游是 SiliconFlow 的 OpenAI-compatible Embeddings，模型 `BAAI/bge-m3`，维度 1024。使用 `SILICONFLOW_API_KEY`；`SILICONFLOW_BASE_URL` 可覆盖默认地址。密钥读取来自当前进程环境，不是从 `.env.example` 自动获得。

### 12.2 上游返回顺序如何保证

`EmbeddingClient.embed_texts()` 检查：

1. 输入不能包含空白正文。
2. 返回数量必须与输入数量相同。
3. 每个响应 index 必须是有效整数，不能是 bool，不能越界。
4. index 不能重复，必须覆盖全部输入位置。
5. 按 index 重建输入顺序。
6. 每个向量长度必须为 1024。

不要把以上检查扩大理解为已经校验了向量所有数值的有限性，或者验证了模型语义质量；当前代码没有做这些检查。

### 12.3 `zip(..., strict=True)` 的作用

```python
for row, vector in zip(chunks, vectors, strict=True):
    ...
```

逐个配对 Child 与向量。数量不同会报错，避免普通 zip 默默截断较长列表。

$$
|chunks|=|vectors|
$$

数量相同本身不证明顺序正确；默认 EmbeddingClient 的 index 检查和重排负责顺序正确。自定义注入客户端也必须遵守该契约。

### 12.4 写入载荷的全部字段

| 字段 | 用途 |
|---|---|
| chunk_id | Milvus 稳定主键，重试 upsert 使用相同身份 |
| document_id、document_version | 定位原文及不可变版本 |
| document_type | 文档分类和筛选 |
| title、section_path、content | 标题、章节和实际搜索正文 |
| model_id | 关联业务模型和过滤 |
| source_uri | 来源地址 |
| page_start、page_end | 原文页码范围 |
| dense_vector | 正文的向量表示 |

index_profile 在这里通过集合归属隔离，不是这份 rows 字典中的字段。PG 的 ingestion_status 也不会写入这份 Milvus 载荷。

### 12.5 ensure、upsert、flush 的执行顺序

```text
ensure_collection
    ↓
upsert_chunks
    ↓
flush
```

`ensure_collection()` 对已有目标检查 profile 归属并 load；不存在时按当前固定 schema 创建。它不是通用 schema migration 工具，不会把任意旧 schema 自动升级为当前 schema。

`upsert` 使用稳定 chunk_id：不存在则写入，存在则更新同一主键。不会替换整个 collection，也不会自动删除多余旧 Child。

`flush` 是本项目确认批次写入完成后才允许推进 PG 状态的步骤。它不负责 PG commit，不自动执行 serving 验收或路由激活；后续验收使用 Strong 一致性读取检查实际载荷。

## 13. 为什么同步 Milvus API 要交给线程

```python
await milvus.acall(write_batch)
```

Milvus 客户端调用是同步的。如果直接放在 async 函数中执行网络等待，会阻塞事件循环。

`acall()` 通过 `run_in_executor()` 交给专用线程池执行，当前 `max_workers=1`。当前协程等待结果时，事件循环可以调度其他协程。

```text
事件循环：提交任务 → await 等待 → 继续处理其他协程
专用线程：ensure → upsert → flush → 返回结果
```

一个线程池只有一个 worker，不表示整个部署只有一个入库任务；其他进程有各自线程池，PG advisory lock 负责应用路径中的跨进程协调。

取消 await 也不能视为已经撤销同步线程里的外部写入，因此失败后仍应按“不确定是否已完成部分写入”处理。

## 14. PG 事务和 Milvus 写入为什么分开

```text
事务 A：登记 PG collection binding → commit

事务 B：guard 持有物化锁
         Embedding API
         Milvus upsert / flush
       → 退出 guard，结束事务

事务 C：标记本批 PG READY → commit
```

事务 B 的 PG 回滚只能回滚 PG 操作、释放 PG 锁，不能撤销已完成的 Embedding 请求或 Milvus 写入。这里没有分布式事务。

`async with sessions()` 管理 Session 生命周期；显式的 `session.begin()` 管理事务的成功提交和异常回滚。`flush()` 只是把当前 SQL 操作发送给数据库，不代表事务已提交。

项目默认 session factory 设置 `expire_on_commit=False`。读取 Child 后离开会话，当前代码使用已加载的普通列构造载荷，不依赖后续懒加载关系。自定义注入会话也应保证这些属性可用。

## 15. 为什么必须先写 Milvus，再标 READY

要求的成功顺序是：

$$
Embedding\ success\rightarrow Upsert\ success\rightarrow Flush\ success\rightarrow PG\ READY\ commit
$$

如果反过来先标 READY，进程随后在写 Milvus 前崩溃，就会出现“PG 声称完成，但搜索副本缺失”，普通 PENDING 重试还找不到这批数据。

现在选择保守语义：即使外部写入可能已经成功，只要 PG 没有记录完成，就允许重写相同 ID。

## 16. 失败位置与恢复行为

| 失败位置 | PG 状态 | Milvus 可能情况 | 下一次处理 |
|---|---|---|---|
| 预检失败 | 不留下本次新绑定 | 不修改 | 修正目标后重试 |
| 绑定失败 | 当前绑定事务回滚 | 尚未本批写入 | 修正归属或用途 |
| Embedding 失败 | 本批仍 PENDING | 本批尚未 upsert | 重新向量化 |
| ensure 失败 | 本批仍 PENDING | 集合可能已存在 | 修正目标再重试 |
| upsert 失败 | 本批仍 PENDING | 可能没有、部分或全部已写 | 相同 ID 重写 |
| flush 失败 | 本批仍 PENDING | 数据可能已经存在 | 相同 ID 重写并 flush |
| flush 后进程中断 | 本批仍可能 PENDING | 本批已写入 | 重写再标 READY |
| READY 事务失败 | 本批更新未成功提交或结果未知 | 本批已写入 | 根据 PG 现状重试 |
| 后续批次失败 | 先前成功批次保持 READY | 先前批次保留 | 后续调用只读剩余 PENDING |

整个 ingest 调用不是“所有批次一起成功或一起回滚”。每批独立推进，不会因为第二批失败就撤销第一批。

异常向上传播；模块没有在函数内部吞掉错误并自动无限重试。这里说的重试包括调用方再次调用或任务系统再次执行。

## 17. 幂等、稳定 ID 和重复计费

把一次固定载荷的 upsert 记为 $U$，Milvus 状态记为 $M$：

$$
U(U(M))=U(M)
$$

含义是：相同 ID、相同载荷重复 upsert，不新增第二个不同主键的 Child。

这个公式的前提是重复操作针对相同载荷。Embedding 外部服务可能重复执行，向量也不能仅凭稳定 Child ID 就宣称逐位相同。实际保障重点是搜索记录身份不重复、业务来源不被错误替换。

稳定 ID 解决“写的是谁”；PG staging 的不可变与摘要检查解决“同一身份是否被换了内容”；两者职责不同。

幂等不等于 Embedding API 恰好调用一次，也不等于恰好计费一次。

## 18. 并发 ingestion 的一个容易忽略的细节

`list_pending_chunks()` 不抢占任务、不加行锁。读取发生在 collection_write_guard 之前，所以两个调用可能读到同一批 PENDING。

```text
A 读取 C01、C02
B 读取 C01、C02
A 获得物化锁并写入
B 之后获得物化锁，仍可能再次写入已读取的数据
```

guard 防止实际写入与验收交错，但没有消除重复读取和重复 Embedding。稳定 ID upsert 与条件 READY 更新使重复处理可以收敛。

因此不能把当前设计描述成“每个 Child 只会被一个 worker 领取”，也不能把 collection 锁理解成去重计费机制。

## 19. `rebuild_collection()`：为什么另有一条重建路径

增量处理只读 PENDING。若 Milvus 集合丢失，PG 中很多 Child 已是 READY，继续调用 ingest 不会重新写这些行。

重建必须从 PG 的全部持久化 Child 出发：

$$
RebuildSource(p)=P_p\cup R_p
$$

重建不重新运行切分策略。这样不会因为未来算法或依赖变化而生成不同 Child，也不会破坏已经封板的构建结果。

## 20. 重建的完整步骤

```text
校验参数、预检 Milvus
    ↓
PG 登记 REBUILD 归属并提交
    ↓
目标已经有 acceptance？
    ├── 是：重新完整验收，返回来源数量，不覆盖写入
    └── 否：记录起始来源数量 expected_count
                ↓
          按 chunk_id 游标分批读取全部 Child
                ↓
          每批 guard → Embedding → ensure → upsert → flush
                ↓
          更新 written 和游标
                ↓
          结束时再次统计来源数量
                ↓
          完整 serving 验收并保存 acceptance
                ↓
          返回 written，不自动激活
```

### 20.1 为什么重建不更新 READY

READY 追踪的是 ingestion 完成状态。写入一个 REBUILD 副本不能冒充已经写入该 profile 的 INGESTION target。

所以原来 PENDING 的 Child 即使被 rebuild 成功写入，PG ingestion_status 也仍然是 PENDING。

### 20.2 应选择什么目标

使用新空集合，或者重试同一个未验收的中断目标。不能把任意含有旧数据的集合当作会被整体清空的目标。

upsert 不删除额外旧行；验收会拒绝多余数据，模块不会自动清理它们。

## 21. 游标分页：`after_chunk_id`

假设排序后的 ID 是：

```text
C01 C03 C07 C09 C12
```

批大小为 2：

```text
第一批：无游标             → C01 C03
第二批：chunk_id > C03    → C07 C09
第三批：chunk_id > C09    → C12
第四批：chunk_id > C12    → 空
```

若上一批末尾 ID 为 $k_t$：

$$
Batch_{t+1}=First_B\left(Sort_{id}\{c\in S_p\mid id(c)>k_t\}\right)
$$

严格大于使上一批最后一行不会再次出现。它按字符串比较，不是把 ID 转成整数，也不是按原文位置分页。

这是 keyset 分页，不使用 OFFSET。但分页本身没有给整个 rebuild 创建一个固定数据库快照；各批读取使用不同会话，最终验收负责检查实际结果。

## 22. 空来源如何处理

如果第一批就是空：

```python
if written == 0:
    await milvus.acall(ensure_empty_collection)
```

仍然要确保目标集合存在，否则无法验收一个实际不存在的服务候选。

空来源下，目标也必须没有额外 Child，才能验收。若目标已有旧行，不会因为 PG 数量为 0 就自动清空。

## 23. 为什么先比较数量，还要完整验收

设重建开始、结束时 PG 来源数量分别为 $N_0,N_1$：

$$
N_0\neq N_1\Rightarrow Rebuild\ failure
$$

数量变化就直接失败，但：

$$
N_0=N_1\not\Rightarrow SourcePayloads\ identical
$$

例如 PG 应有 C01、C02、C03，Milvus 却有 C01、C02、C04，数量相等仍然错误。

当前 rebuild 没有从开始就持有 profile source 独占锁。最终验收会持有 source 独占锁和物化锁，稳定检查当时的权威来源与实际集合。

这不等于证明重建期间从未发生任何来源变化。例如新增后又删除、或越权改动后恢复，不一定能从首尾计数看出。算法依赖正常路径中的来源不可变约束和最终完整校验。

## 24. serving 完整验收具体检查什么

`validate_serving_collection()` 在 `serving.py`：

1. 获取 profile source 独占锁。
2. 获取与写入相同的 collection 物化锁。
3. 检查 PG binding 存在、profile 一致、purpose 为 REBUILD。
4. 检查 Milvus 物理集合存在且归属兼容。
5. Strong 一致性读取实际数量。
6. 分批读取 PG Child，按 ID 从 Milvus 取实际载荷。
7. 逐字段比较 `milvus.OUTPUT_FIELDS` 中的来源与正文业务字段。
8. 检查缺失、重复、多余和最终数量。
9. 计算摘要，保存成功 acceptance。

设 PG 预期 Child 载荷集合为 $A$，Milvus 对应业务载荷集合为 $M$，目标是确认：

$$
IDs(A)=IDs(M)
$$

$$
\forall id\in IDs(A),\quad payload_A(id)=payload_M(id)
$$

实际校验字段不包含 dense_vector、ingestion_status、数据库时间字段，也不包含 Parent/Link。因此验收并不证明向量由指定模型正确计算，也不对向量逐位重新计算比较。

## 25. 摘要公式与验收记录

将 profile 和按 chunk_id 排序的 Child 业务载荷序列化，摘要近似表达为：

$$
D=SHA256\left(UTF8\left(p+\texttt{LF}+J(c_1)+\texttt{LF}+\cdots+J(c_N)+\texttt{LF}\right)\right)
$$

其中 $J$ 是键排序、紧凑格式、保留中文的 JSON 序列化，`LF` 为换行字符；$p$ 是 profile 字符串。空来源也包含 profile 与其后的换行。

验收记录包含 collection、profile、来源数量、来源摘要、验证摘要、完成标记、PASSED 状态和时间等。

摘要用于标识通过验收的来源载荷；它不是加密签名，也不能在数据库和摘要都被越权修改时独立证明真实性。

只有成功验收才留下合格记录；失败不会伪装为 PASSED。相同来源的重复验收返回原有记录，但仍会执行实际数据检查。

## 26. 已验收目标再次 rebuild 会怎样

发现 acceptance 后，不会重新 Embedding，也不会重新 upsert，而是重新执行验收：

```text
原来源与实际集合仍一致 → 返回验收记录中的 source_chunk_count
来源已经变化或集合被破坏 → 报错
```

来源新增后，应构建新的 REBUILD 集合。旧集合仍可作为原 serving snapshot 使用；“旧集合不能重新验收当前来源”不等于它会自动下线。

新建路径返回 `written`，重试已验收路径返回 receipt 的来源数量；它们都不是“本次新插入的 Milvus 唯一主键数量”。

## 27. 验收、激活和检索如何连接

```text
rebuild_collection()
    ↓ 内部调用 validate_serving_collection()
验收通过，保存记录并冻结
    ↓ 单独显式调用 activate_serving_collection()
校验权限、预期路由版本、来源与实际集合
    ↓
PG 保存激活审计和当前路由
    ↓
检索开始 resolve_serving_route(profile)，只读取一次
    ↓
RetrievalRouteSnapshot(profile, collection, route_version)
    ↓
Query Embedding → snapshot.collection 搜索
    ↓
PG Child 权威校验 → Context Resolution
```

没有 serving route 时明确失败，不回退旧 active alias。请求期间路由切换不会改变已经取得的 snapshot。

Baseline 返回 Child Context；Parent-Child 在 PG 根据 Child→Parent Link 解析 Parent，并按首次 Child rank 去重。这些都不在 ingestion.py 中实现。

## 28. 一个完整生命周期例子

```text
1. 文档 A 构建并 stage：C01、C02，状态 PENDING
2. ingest_pending(collection=ingestion_A)：两行转 READY
3. ingestion_A 不允许 serving 验收
4. rebuild_collection(collection=rebuild_v1)：写入 C01、C02 并验收
5. 显式激活 rebuild_v1：route_version = 1
6. 文档 B stage：C03，状态 PENDING
7. ingest_pending(collection=ingestion_A)：C03 转 READY
8. 线上路由仍为 rebuild_v1，仍只包含原来的 C01、C02
9. rebuild_collection(collection=rebuild_v2)：写入 C01、C02、C03 并验收
10. 显式激活 rebuild_v2：route_version = 2
```

关键规则：持续知识更新不自动改变线上检索快照；只有重建、验收、显式激活才能切换线上版本。

## 29. 性能和资源如何理解

每批时间可粗略拆成：

$$
T_{batch}\approx T_{read}+T_{lockwait}+T_{embedding}+T_{ensure}+T_{upsert}+T_{flush}+T_{status}
$$

重建还要加最终完整验收时间；重建没有每批 READY 状态更新。这里只是分析模型，不是已测量的性能结果。

本批向量数值规模约为：

$$
V=B\times1024
$$

如果只按 float32 原始值估算：

$$
Bytes_{raw}=B\times1024\times4
$$

$B=32$ 时约 128 KiB，$B=64$ 时约 256 KiB。Python 列表、float 对象、正文、JSON 和网络缓冲都会产生额外开销，这不是进程真实内存占用。

批次更大不保证更快：上游 token 限制、请求延迟、限流、失败重试成本和 Milvus flush 成本都会影响结果。当前函数本身不实现自适应批大小或专门的限流调度。

## 30. 阅读本文件需要的 Python 语法

| 写法 | 含义 |
|---|---|
| `async def` | 定义协程函数，调用后需 await 或由事件循环执行 |
| `await operation()` | 等待异步操作，不表示自动并行执行后续语句 |
| `async with` | 异步上下文管理，负责进入和退出清理 |
| 参数列表中的 `*` | 后续参数必须使用参数名传入 |
| `X \| None` | 允许 X 类型或 None 的类型注解 |
| `list[KnowledgeChunkRow]` | 元素为 ORM Child 行的列表类型注解 |
| `[row.content for row in chunks]` | 列表推导，按顺序提取正文 |
| `for row, vector in zip(...)` | 同时解包一对 Child 和向量 |
| `chunks[-1]` | 列表最后一个元素，当前代码在非空批次后使用 |
| `while True` | 持续循环，依赖内部 return 或异常退出 |
| `completed += n` | 累加本次实际成功更新数量 |
| 嵌套 `def write_batch()` | 闭包引用外层 rows、collection、profile 等变量 |
| `raise ValueError(...)` | 当前输入不符合要求，异常向上传递 |
| `raise RuntimeError(...)` | 当前执行条件不满足，如重建期间数量变化 |

嵌套函数并没有在定义时就执行 Milvus 操作，而是在传给 `acall()` 后由执行线程调用。

## 31. 当前边界与不能误读的保证

- PG 与 Milvus 没有分布式事务；PG 回滚不能撤销 Milvus 外部副作用。
- preflight 与 PG bind 之间仍有外部竞争窗口，写前归属检查防止向错误 owner 写入，但不能保证完全不留下绑定。
- 并发读取 PENDING 可能重复调用 Embedding；不保证外部调用恰好一次。
- 集合冻结依赖应用 guard 协议，不能阻止越权外部写入。
- 完整验收期间持有 profile source 独占锁并访问 PG/Milvus，数据量增大后会增加阻塞时间。
- legacy 无 marker Baseline adoption 是迁移兼容策略，后续应退役。
- Parent/Link 不在当前 serving Child 摘要中，依赖应用层不可变与解析时 lineage 检查。
- 完整验收验证存储一致性，不验证检索召回率、上下文质量或答案正确率。
- 本文件不新增 BM25、Reranker、Query Rewrite，也不承担业务工作流编排。

## 32. 测试如何帮助理解

`backend/tests/knowledge/test_ingestion.py` 中可以重点看：

| 测试 | 锁定的行为 |
|---|---|
| `test_ingest_pending_batches_and_marks_ready_after_flush` | 按批处理，flush 后才 READY |
| `test_failed_flush_leaves_batch_pending_for_idempotent_retry` | flush 失败后保留 PENDING |
| `test_failed_upsert_skips_flush_and_ready_then_retries` | upsert 异常不继续标完成 |
| `test_second_batch_failure_preserves_first_batch_and_retries_only_pending` | 前批成功不被后批失败撤销 |
| `test_embedding_failure_does_not_write_or_mark_ready` | Embedding 失败不写 Milvus、不标 READY |
| `test_ingestion_rejects_alias_and_invalid_batch_size` | 入口拒绝旧 alias 和超范围批大小 |

跨 profile 归属、真实 Milvus 和 serving 生命周期还要结合 `test_profile_milvus.py`、`test_serving.py` 等测试阅读。部分真实服务测试依赖环境开关和运行中的服务，不能把跳过当作通过。

本文整理不重新宣称已经运行这些测试；本次仅新增文档并检查文档结构，不修改业务行为。

## 33. 推荐的源码对照顺序

1. [ingestion.py](../backend/src/risk_platform/knowledge/ingestion.py)：先看两个主入口。
2. [embeddings.py](../backend/src/risk_platform/knowledge/embeddings.py)：理解正文如何生成并校验向量。
3. [milvus.py](../backend/src/risk_platform/knowledge/milvus.py)：理解 collection、owner marker、upsert 和线程桥接。
4. [repository.py](../backend/src/risk_platform/knowledge/repository.py)：对照 bind、pending、rebuild 和 READY 更新。
5. [serving.py](../backend/src/risk_platform/knowledge/serving.py)：最后深入 guard、验收和激活。
6. [retrieval.py](../backend/src/risk_platform/knowledge/retrieval.py)：理解路由 snapshot 下的 Child 搜索。
7. [context_resolution.py](../backend/src/risk_platform/knowledge/context_resolution.py)：理解 Child 如何变成最终上下文。

配套文档：[Baseline 切分指南](knowledge-chunking-guide.md)、[Parent-Child 构建指南](knowledge-parent-child-guide.md)、[数据库持久化指南](knowledge-persistence-guide.md)。

## 34. 最后用六句话复述整份代码

1. 权威 Child 已保存在 PG，入库和重建不重新切分。
2. 增量只读 PENDING，Milvus 写入和 flush 成功后才提交 READY。
3. 稳定 ID upsert 支持重复处理，但不保证 API 只调用或计费一次。
4. 重建读取 PENDING 和 READY，按 ID 游标分批写入，不修改 ingestion 状态。
5. REBUILD 通过完整验收后冻结，INGESTION 保持增量可写。
6. 验收与激活分开，线上查询只使用显式激活路由中的物理集合。
