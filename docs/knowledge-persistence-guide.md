# RiskMind Knowledge 数据库层：算法、事务与一致性详解

> 本文对应当前仓库的实现，主要讲 PostgreSQL（简称 PG）中的知识持久化。
> 不修改表结构或业务算法。SQL 片段除明确标注外均为帮助理解的示意，不是生产执行脚本。
> 数学公式使用 `$...$` 和 `$$...$$`，需使用支持 LaTeX 的 Markdown 阅读器。

## 目录

1. [职责与代码地图](#1-职责与代码地图)
2. [数据库连接和会话](#2-数据库连接和会话)
3. [ORM 与数据库语法](#3-orm-与数据库语法)
4. [九张表及其关系](#4-九张表及其关系)
5. [事务边界与原子性](#5-事务边界与原子性)
6. [规范 JSON、摘要与幂等](#6-规范-json摘要与幂等)
7. [完整 staging 流程](#7-完整-staging-流程)
8. [Child 写入与业务字段比较](#8-child-写入与业务字段比较)
9. [锁的类型、键与生命周期](#9-锁的类型键与生命周期)
10. [物理 collection 绑定](#10-物理-collection-绑定)
11. [查询、排序和结果提取](#11-查询排序和结果提取)
12. [游标分页与 rebuild 数据读取](#12-游标分页与-rebuild-数据读取)
13. [状态更新和跨存储重试](#13-状态更新和跨存储重试)
14. [Rebuild、验收和线上激活](#14-rebuild验收和线上激活)
15. [完整生命周期示例](#15-完整生命周期示例)
16. [迁移和数据库约束](#16-迁移和数据库约束)
17. [实现边界与常见误解](#17-实现边界与常见误解)
18. [可运行的离线示例](#18-可运行的离线示例)
19. [测试与复习清单](#19-测试与复习清单)

## 1. 职责与代码地图

先建立主线：

```text
Document → 分块策略 → PreparedKnowledge
                            ↓ repository.stage_prepared()
                 PostgreSQL 权威数据
                            ↓ ingestion
                 Milvus 可重建搜索副本
                            ↓ validate / activate
                 profile 对应的 serving route
```

| 文件 | 职责 |
|---|---|
| [`db/database.py`](../backend/src/risk_platform/db/database.py) | Engine、Session 工厂与清理 |
| [`knowledge/models.py`](../backend/src/risk_platform/knowledge/models.py) | 表、列、主键、外键、CHECK 和索引 |
| [`knowledge/repository.py`](../backend/src/risk_platform/knowledge/repository.py) | 权威数据读写、幂等和冲突检查 |
| [`knowledge/ingestion.py`](../backend/src/risk_platform/knowledge/ingestion.py) | PG Child 到 Milvus 的增量写入及重建 |
| [`knowledge/serving.py`](../backend/src/risk_platform/knowledge/serving.py) | 验收、写保护、激活历史与当前路由 |
| [`knowledge/cli.py`](../backend/src/risk_platform/knowledge/cli.py) | 部分调用入口及事务组织 |

`repository.py` 不是分块模块，不调用 Embedding 或写 Milvus。
它保存准备结果，并检查“输入是不是合法结果、是不是同版本改写、是不是一致重试”。

这里的算法主要是事务协调、身份比较、增量状态推进、游标遍历和快照验收，而不是模型算法。

## 2. 数据库连接和会话

### 2.1 Base、Engine、Session 的区别

```text
Base：ORM 模型共同基类，登记表结构元数据
Engine：连接数据库的引擎及连接池
Session 工厂：每次需要操作时创建会话
Session：一次或一组数据库操作使用的工作单元
Transaction：一批修改提交或回滚的边界
```

`DATABASE_URL_ENV = "RISK_DATABASE_URL"` 指定连接配置的环境变量名。
缺少配置时明确失败，不自动连接某个默认生产库。

### 2.2 单例引擎与独立会话

`get_engine()` 第一次被调用时创建异步 Engine，后续在当前进程复用。
`pool_pre_ping=True` 在取连接时检查失效连接，不等同于自动重试整笔业务。

`get_session_factory()` 创建并缓存 Session 工厂，不是创建一个全局永不关闭的 Session。

```python
sessions = get_session_factory()
async with sessions() as session:
    ...
```

`expire_on_commit=False` 使已提交 ORM 对象的属性不立即失效，但不意味着这些属性永远与数据库最新值一致。
需要刷新时仍需显式读取或使用 `populate_existing=True`。

## 3. ORM 与数据库语法

### 3.1 类、对象和数据行

```python
class KnowledgeChunkRow(Base):
    __tablename__ = "knowledge_chunks"
```

类描述表，实例表达某行数据，`__tablename__` 是实际 SQL 表名。
这与 Pydantic 的 `KnowledgeChunk(BaseModel)` 不同：前者是数据库映射，后者是传输与校验契约。

```python
content: Mapped[str] = mapped_column(Text, nullable=False)
```

- `Mapped[str]`：声明 Python ORM 属性类型。
- `mapped_column()`：声明数据库列。
- `Text`：数据库文本类型，不是 Python 分块长度限制。
- `nullable=False`：拒绝 SQL NULL，不自动拒绝空字符串或纯空白。

### 3.2 主键、外键、CHECK、唯一约束、索引

| 定义 | 作用 |
|---|---|
| `primary_key=True` | 标识记录；多列声明时共同组成一个联合主键 |
| `ForeignKeyConstraint` | 约束关联的目标记录必须存在 |
| `UniqueConstraint` | 约束指定字段组合不可重复 |
| `CheckConstraint` | 每行必须满足 SQL 条件 |
| `Index` | 改善查询；`unique=True` 时也约束唯一性 |

两个字段都标 `primary_key=True`，不是要求每个字段单独唯一，而是组合唯一。
复合外键也必须作为一个组合引用同一目标记录，不能把各列拆开理解。

例如：

```python
CheckConstraint(
    "index_profile IN ('baseline-v1', 'parent-child-v1')",
    name="ck_knowledge_builds_profile",
)
```

限制合法 profile，并为约束命名以便定位和迁移。
CHECK 不应单独当成 NOT NULL 使用；本项目该列另外受主键或非空约束保护。

### 3.3 默认值和时间

| 配置 | 含义 |
|---|---|
| `default=...` | SQLAlchemy 插入路径在未提供值时使用 |
| `server_default=...` | 数据库 INSERT 省略该列时使用 |
| `server_default=func.now()` | 插入时由数据库生成时间 |
| `onupdate=func.now()` | SQLAlchemy 更新路径补入时间，不是数据库通用触发器 |
| `DateTime(timezone=True)` | PostgreSQL 使用带时区语义的时间列 |

这些默认值不等于应用永远不能显式提供字段，也不代替状态机的调用顺序。

## 4. 九张表及其关系

### 4.1 身份维度

| 字段 | 表示什么 |
|---|---|
| `document_id` | 哪份文档 |
| `document_version` | 文档内容哪个版本 |
| `index_profile` | 按什么固定构建方案生成索引 |
| `model_id` | 文档讨论哪个风控模型，例如 credit_v3，不是 Embedding 模型 |
| `chunk_id` | 哪个搜索 Child |
| `context_id` | 哪个 Parent |
| `route_version` | 该 profile 的第几次成功线上切换 |

### 4.2 表结构速查

| 表模型 | 主键 | 主要内容 |
|---|---|---|
| `KnowledgeDocumentRow` | 文档 ID + 文档版本 | canonical 原文 JSON，跨 profile 共享 |
| `KnowledgeBuildRow` | profile + 文档 ID + 版本 | 单份文档准备结果摘要 |
| `KnowledgeChunkRow` | chunk_id + profile | Child 业务正文、来源、状态与时间 |
| `KnowledgeContextRow` | profile + context_id | Parent 正文与来源 |
| `KnowledgeContextLinkRow` | profile + chunk_id | Child 所属 Parent、父内 ordinal |
| `KnowledgeCollectionRow` | collection | 物理 collection 的归属与用途 |
| `KnowledgeCollectionBuildRow` | build_id | collection 成功验收及来源摘要 |
| `KnowledgeServingActivationRow` | idempotency_key | 激活历史与原请求回执 |
| `KnowledgeServingRouteRow` | profile | 当前线上目标 |

### 4.3 关联关系

```text
Document（文档 ID、版本）
    ↓ Build 外键
Build（profile、文档 ID、版本）
    ↓ Parent 外键
Parent ← Link → Child

Collection（collection、profile）
    ↓ 复合外键
CollectionBuild（build_id、profile、collection）
    ↓
Activation（历史） ← Route 的 profile/version 外键
    ↑
Route 还直接引用 CollectionBuild 的三列身份
```

注意实际模型中，Child 的 `document_id/document_version` 没有直接声明到 Document 的外键。
不能把业务关系图误认为每一条关系都由数据库 FK 强制保证。

Link 的主键保证一个 Child 在该 profile 下至多关联一个 Parent；
“每个 Parent-Child Child 必须有 Link”、父子来源一致等更强规则由构建契约和业务代码检查。

### 4.4 profile 隔离

```text
同一文档 CASE-001/v1
    ├─ baseline-v1 的构建产物
    └─ parent-child-v1 的构建产物
```

原文共享，产物按 profile 隔离。这样不同策略可以比较，也不会把父子 Link 接到另一种方案上。

## 5. 事务边界与原子性

### 5.1 add、flush、commit 的区别

| 操作 | 含义 | 是否代表整笔事务成功 |
|---|---|---|
| `session.add()` / `add_all()` | 登记待保存对象 | 否 |
| `await session.flush()` | 发送待处理 SQL、检查约束 | 否 |
| `await session.commit()` | 提交事务 | 成功返回后代表提交完成 |
| `await session.rollback()` | 回滚事务 | 撤销未提交修改 |

Session 还可能在查询前自动 flush，不能把“还没手写 flush”理解成一定没有 SQL 执行。
真正的持久化边界是事务，而不是某次 add 或 flush。

### 5.2 repository 不提交

推荐的调用组织方式：

```python
async with sessions() as session:
    async with session.begin():
        count = await stage_prepared(
            session, document, prepared,
            index_profile=IndexProfile.PARENT_CHILD,
        )
```

这是调用结构示意，需要调用方已经配置会话工厂并构造好 document/prepared。

```text
正常退出 begin → COMMIT
异常传播出 begin → ROLLBACK
```

即使 `return` 写在上下文内部，Python 也会先执行上下文退出逻辑再真正返回。
单纯退出一个 Session 上下文不能当作自动提交；提交由事务上下文或显式 commit 决定。

### 5.3 为什么不能仓储内部各自 commit

一次 staging 涉及多张表：

```text
Document → Build → Child → Parent → Link
```

必须在调用方同一事务中成功提交，或出错时整体回滚。
若每张表各自提交，Link 失败时可能留下没有完整映射的半套产物。
调用方不能吞掉冲突异常后继续提交原本应失败的事务。

## 6. 规范 JSON、摘要与幂等

### 6.1 `_canonical()` 不计算哈希

```python
json.dumps(
    value.model_dump(mode="json"),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)
```

- `mode="json"` 将枚举等变成 JSON 值。
- `sort_keys=True` 消除字典键排列差异。
- 紧凑分隔符消除 JSON 排版空格。
- 保留中文、正文字符串内容以及数组顺序。

它不是对正文做 strip，不进行 Unicode 等价归一化，也不自动排序 Child 列表。

### 6.2 构建摘要

定义规范编码函数为 $J$，准备结果为 $A$：

$$
D(A)=\operatorname{hex}\left(\operatorname{SHA256}(\operatorname{UTF8}(J(A)))\right)
$$

`prepared_digest` 覆盖 `search_chunks`、`context_units`、`context_links` 及其顺序。

假设构建身份为：

$$
K=(\text{profile},\text{document ID},\text{document version})
$$

则 staging 决策为：

$$
\operatorname{stage}(K,A)=
\begin{cases}
\text{insert},&K\text{ 不存在}\\
\text{no-op},&K\text{ 已存在且 }D(A)=D_{old}\\
\text{conflict},&K\text{ 已存在且 }D(A)\neq D_{old}
\end{cases}
$$

### 6.3 为什么不能只比较 ID

构建身份回答“你声称提交哪一次构建”，不能证明所有 Child 元数据、Parent、Link 和顺序都相同。
Parent/Child 的内容型 ID 也没有覆盖所有来源元数据和整个结果集合。

因此分工是：

```text
ID：找到旧记录
固定策略复算：判断输入符合当前 profile 语义
完整摘要：判断与历史准备结果是否一致
```

### 6.4 幂等要解决的场景

```text
PG 已提交成功
    ↓
调用方没收到响应，或任务被重复执行
    ↓
再次提交相同文档及构建结果
    ↓
返回 0，不重复插入，不重置 READY
```

摘要不是防篡改或自动修复机制。命中已有 Build 时，当前函数没有重新读取全部 Parent/Child/Link 校验实际存储；
它依赖正常业务路径的不变性，不会自动修复人工越权修改后的数据。

## 7. 完整 staging 流程

入口：`stage_prepared(session, document, prepared, index_profile=...)`。

### 7.1 检查输入和固定策略

1. 转换 `IndexProfile`，拒绝未支持的值。
2. 将 document/prepared 序列化后重新校验并复制。
3. 根据 profile 再运行对应纯构建策略。
4. 要求传入 prepared 与预期结果完全相同。
5. 拒绝没有搜索 Child 的构建。

staging 复算策略是输入完整性检查；不要与 Milvus rebuild 混淆，后者读取持久化 Child，不重新切文档。

### 7.2 文档级锁和原文检查

以 `(document_id, document_version)` 构造跨 profile 共用的文档锁，然后查询 `KnowledgeDocumentRow`。

```text
原文记录存在且 JSON 相同 → 继续
原文记录存在但 JSON 不同 → 文档版本冲突
原文记录不存在 → 走首次登记或旧数据兼容检查
```

旧 Stage 5 可能只有 Baseline Child，没有 canonical 原文。
此时读取同文档版本的旧 Baseline Child，要求新原文可通过固定 Baseline 规则重现全部业务字段，才允许补登记。

### 7.3 构建摘要比较

按照 `(profile, document_id, document_version)` 查询 Build。

- 已有摘要相同：返回 0，停止本次写入。
- 已有摘要不同：冲突，要求新文档版本或新的合法方案身份。
- 没有 Build：准备首次保存。

### 7.4 写入依赖顺序

```text
登记 Document 并 flush
    ↓
登记 Build 并 flush
    ↓
_stage_chunks() 保存 Child
    ↓
保存 Parent 并 flush
    ↓
保存 Link 并 flush
    ↓
返回新增 Child 数量
    ↓
调用方提交事务
```

Build 先于 Parent，Parent/Child 先于 Link，满足当前复合外键约束。
Baseline 的 Parent/Link 列表为空，不需要虚构这些行。

## 8. Child 写入与业务字段比较

### 8.1 `_stage_chunks()` 的输入检查

- 至少一个 Child。
- 全部 Child 属于同一文档 ID 与版本。
- 当前批次 Child ID 不重复。

“批次”在这里是一份文档版本的完整 staging 输入，不能误解成可任意分几次补入该版本的 Child。
已有部分记录而本次集合不相同，会被判为版本冲突，不做静默补齐。

### 8.2 锁与兼容校验

先取得文档版本锁，再取得 profile 来源共享锁。
若为 Baseline 且已有 canonical 原文，旧入口也必须匹配该原文的固定 Baseline Chunk 集合。

### 8.3 新旧业务字段

`_chunk_business_values()` 与 `_row_business_values()` 使用同样的字段顺序：

```text
chunk_id、document_type、title、section_path、content、model_id、
source_uri、page_start、page_end
```

前者接收 Pydantic Chunk，将文档类型枚举取 `.value`；后者接收 ORM 行，类型本身已经是数据库字符串。
文档 ID、版本、profile 由外围输入和查询条件限定。

不比较状态、创建时间或 READY 时间，因为业务正文不变时这些处理信息允许改变。

```text
新旧业务集合一致 → 返回 0
新旧业务集合不同 → DocumentVersionConflictError
没有旧记录 → 新增全部输入 Child，初始 PENDING
```

这里排序后比较业务元组，忽略输入列表排列；完整准备结果的顺序另由上层 prepared_digest 保护。

### 8.4 旧入口 `stage_chunks()`

兼容 Baseline-only 写入，拒绝 `pc-child-` 前缀的 Parent-Child 输出，委托 `_stage_chunks()`。
它不会自行创建完整 Document/Build/Parent/Link，不能拿它代替完整策略 staging。

## 9. 锁的类型、键与生命周期

### 9.1 使用的是 PostgreSQL advisory lock

```sql
SELECT pg_advisory_xact_lock(hashtextextended(:key, 0));
```

锁键由业务字符串映射为数据库整数键。数据库并不知道字符串代表一份报告，
是所有参与者约定使用相同锁键实现协调。
这不是直接锁定表中某行，也不会自动阻止绕过约定的任意 SQL。

### 9.2 锁键与职责

| 锁键模板 | 申请位置 | 主要用途 |
|---|---|---|
| `riskmind:knowledge:{document_id}:{version}` | stage_prepared / _stage_chunks | 同文档版本串行检查，不含 profile |
| `riskmind:knowledge-source:{profile}` | lock_profile_source | staging 与全量验收、激活协调 |
| `riskmind:knowledge-index:profile:{profile}` | bind_collection | 同策略绑定协调 |
| `riskmind:knowledge-index:collection:{collection}` | bind_collection | 同物理目标归属竞争 |
| `riskmind:knowledge-materialization:{collection}` | serving 写保护与校验 | 写入与验收互斥 |
| `riskmind:serving-activation-key:{key}` | activate_serving_collection | 同幂等激活键串行处理 |

以上只是该业务层的 advisory locks；数据库执行 INSERT/UPDATE、外键校验等还会使用自己的常规锁。

### 9.3 共享与独占

`lock_profile_source(..., shared=True)` 使用 `pg_advisory_xact_lock_shared()`。
默认 `shared=False` 使用独占形式。

| 已持有模式 | 另一事务申请共享 | 另一事务申请独占 |
|---|---|---|
| 共享 | 兼容 | 冲突等待 |
| 独占 | 冲突等待 | 冲突等待 |

这里“共享”不表示只能读：多个遵守约定的 staging 可以共享来源锁、写入不同文档。
同文档版本仍由另外的文档锁串行处理。
验收/激活独占来源锁时，新的 Child staging 在取得共享锁前等待。
先前尚未提交的其他表变更，不应误认为已经进入可见权威 Child 集合。

### 9.4 锁顺序

```text
staging：文档锁 → profile 来源共享锁（首次 Child 写入路径）
绑定：profile 绑定锁 → collection 绑定锁
验收：profile 来源独占锁 → collection 物化锁
激活：请求幂等键锁 → profile 来源独占锁 → collection 物化锁
```

已有构建摘要相同、提前返回的 staging 不会进入后续 Child 写入路径。
不要把不同命名空间中都带 profile 的锁误认为同一把锁。
统一锁顺序减少冲突风险，但不能据此宣称任意外部 SQL 都不会发生死锁。

### 9.5 加锁到释放的完整过程

```text
进入调用方 session.begin()
    ↓
执行加锁 SQL
    ├─ 无冲突：取得锁继续
    └─ 有冲突：等待对方结束，或等待被超时/取消打断
    ↓
查询、比较、写入、flush
    ↓
仓储函数 return（锁仍持有）
    ↓
事务正常退出：COMMIT → 自动释放
事务异常退出：ROLLBACK → 自动释放
```

`flush()` 和普通函数返回不会释放事务级锁。
同一事务重复取得同一把文档锁，不会让自己阻塞自己。
不需要调用 `pg_advisory_unlock()`；那不是本项目事务级锁的配对释放方式。

### 9.6 两个请求的时间线

```text
A：取得 CASE-001/v1 文档锁
B：申请同一文档锁，等待
A：保存并提交，释放锁
B：取得锁，查到 A 已提交数据
B：确认完全一致，返回 0
B：结束事务，释放锁
```

锁负责协调检查时机，业务比较负责识别数据是否一致；锁本身不判断正文对错。

### 9.7 与 MySQL 的区别

| 机制 | 对象 | 释放规则 |
|---|---|---|
| 当前 PG 事务级 advisory lock | 应用定义键 | 提交或回滚释放 |
| MySQL InnoDB 锁定读 | 记录/索引范围，受查询和隔离级别影响 | 事务结束释放锁定读持有的锁 |
| MySQL GET_LOCK | 应用定义名字 | RELEASE_LOCK 或连接结束；commit/rollback 不释放 |

参考：[PG advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)、
[MySQL 锁定读](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)、
[MySQL GET_LOCK](https://dev.mysql.com/doc/refman/8.4/en/locking-functions.html)。

## 10. 物理 collection 绑定

`bind_collection()` 只写 PG 归属登记，不创建 Milvus collection，不写向量，不激活路由。

```text
检查 profile 和 purpose
    ↓
按固定顺序取得 profile/collection 绑定锁
    ↓
已绑定同 owner、同用途 → 幂等返回
已绑定不同 owner 或用途 → 失败
未绑定 → 检查 ingestion 唯一性，再插入登记
```

规则：

```text
一个物理 collection 只能登记一个 profile 和用途
一个 profile 最多一个 INGESTION target
一个 profile 可以有多个 REBUILD target
```

部分唯一索引仅作用于 `purpose='INGESTION'` 的行，不是整个 collection 表的 profile 全局唯一。

为何固定 ingestion target？READY 只说明已写入该 profile 的既定增量目标。
如果随意更换目标，旧 READY 记录未必已出现在新目标中，状态含义会混乱。

Milvus owner 只读预检由 ingestion/rebuild 上层在 PG 绑定前执行，真正写入前再次检查。
仍不等同于跨 PG/Milvus 分布式事务，极窄的外部竞争窗口是已知边界。

## 11. 查询、排序和结果提取

### 11.1 SQLAlchemy 链式查询

```python
statement = (
    select(KnowledgeChunkRow)
    .where(KnowledgeChunkRow.index_profile == profile.value)
    .order_by(KnowledgeChunkRow.chunk_id)
    .limit(limit)
)
result = await session.execute(statement)
rows = list(result.scalars())
```

| 写法 | 意义 |
|---|---|
| `select(Model)` | 查询对应模型记录 |
| `.where(...)` | 增加过滤条件，多个条件通常组成 AND |
| `.order_by(column)` | 按列升序排序；降序需要显式 `.desc()` |
| `.limit(limit)` | 限制最多返回多少行 |
| `session.execute()` | 执行查询语句 |
| `result.scalars()` | 提取每个结果的第一列；查询单个模型时就是 ORM 对象 |
| `list(...)` | 将结果物化为列表 |

`order_by(chunk_id)` 的意义是使批次和遍历顺序稳定，不代表原文顺序或搜索相关性顺序。
当前 ID 是带前缀的哈希字符串，比较采用数据库字符串排序规则，不是数字自增计数。

### 11.2 单行查询工具

- `session.get(Model, key)`：按主键获取记录，不存在返回 None；可能利用 Session 的身份缓存。
- `scalar_one()`：要求结果恰好一行，常用于 COUNT 查询。
- `scalar_one_or_none()`：允许零行或一行，多行报错。
- `.scalars().first()`：取得首个标量对象或 None，不表达“必须唯一”的要求。

不要把 `scalars()` 与单一数字混淆，返回什么取决于 select 的内容。

### 11.3 为什么使用 populate_existing

`list_rebuild_chunks()` 和 route 查询部分使用 `populate_existing=True`，
让新查询结果覆盖 Session 身份缓存中的旧属性。它不是开启更强事务隔离，也不是自动获得固定快照。

## 12. 游标分页与 rebuild 数据读取

### 12.1 两种读取目标

| 函数 | 状态范围 | 用途 |
|---|---|---|
| `list_pending_chunks()` | PENDING | 处理尚未完成的增量入库 |
| `list_rebuild_chunks()` | PENDING 和 READY | 重建完整搜索副本 |

读取 PENDING 不等于认领任务：没有在该查询中使用 FOR UPDATE 或 SKIP LOCKED，
不同工作者可能读到同批数据，写入协调和稳定 ID/upsert 由上层处理。

### 12.2 keyset 分页

rebuild 读取的 SQL 示意：

```sql
SELECT *
FROM knowledge_chunks
WHERE index_profile = :profile
  AND ingestion_status IN ('PENDING', 'READY')
  AND chunk_id > :after_chunk_id
ORDER BY chunk_id ASC
LIMIT :limit;
```

首次没有游标条件。后续游标取上一批最后一个 ID，而不是最大原文序号。

设固定数据集合为 $S_p$，当前游标为 $c$，批量上限为 $L$：

$$
Q(c)=\operatorname{take}_L\left(\operatorname{sort}_{id}\{x\in S_p\mid x.id>c\}\right)
$$

新游标：

$$
c_{next}=\operatorname{last}(Q(c)).id
$$

首次相当于不限制下界，空结果表示本轮遍历结束。

采用严格大于，避免重复读取上一页最后一行；不使用 OFFSET。
同一 profile 下主键唯一，使 chunk_id 可作为该范围内的唯一游标。

例如排序结果为：

```text
chunk-001、chunk-002、chunk-003、chunk-004、chunk-005
```

每批两条：

```text
第一批：001、002 → 游标 002
第二批：003、004 → 游标 004
第三批：005      → 游标 005
第四批：空      → 结束
```

### 12.3 分页不自动等于固定快照

上述“不漏不重”的直观例子假设来源集合稳定。
实际 rebuild 在多个会话中分批读取，没有在整个重建期间持有来源独占锁。
并发 staging 可能使集合变化；初始/结束数量检测只覆盖部分变化，最终还要完整验收。
不能只看到 `order_by` 和游标，就宣称构建期间数据绝对不变。

## 13. 状态更新和跨存储重试

### 13.1 PENDING / READY 含义

```text
staging 新 Child → PENDING
Embedding + Milvus ensure/upsert/flush 成功
    ↓
mark_chunks_ready() → READY
```

READY 不表示任意 REBUILD collection 都有该 Child，也不表示已经线上激活。

### 13.2 条件更新保证重复标记无副作用

`mark_chunks_ready()` 先对输入 ID 去重，空集合直接返回零。
UPDATE 同时限制 profile、ID 集合以及原状态 PENDING：

```sql
UPDATE knowledge_chunks
SET ingestion_status = 'READY',
    ready_at = now(),
    updated_at = now()
WHERE index_profile = :profile
  AND chunk_id IN (...)
  AND ingestion_status = 'PENDING';
```

已有 READY 行不重写时间；返回实际更新行数，不是输入 ID 数量。
函数本身不检查 Milvus，必须由调用方在外部写入完成后调用。

### 13.3 没有跨数据库原子事务

PG 事务不能自动撤销已经发生的 Milvus upsert。
当前利用稳定 ID、upsert 和“先写成功再标 READY”的顺序恢复：

| 故障位置 | PG 状态 | 重试行为 |
|---|---|---|
| Embedding 失败 | 仍 PENDING | 再次尝试模型调用 |
| Milvus 写入或 flush 失败 | 仍 PENDING | 再次 upsert 同一 ID |
| Milvus 成功，PG 标 READY 前中断 | 仍 PENDING | 重复写同 ID，再尝试标 READY |
| READY 已提交但调用方丢失响应 | READY | 后续 pending 查询不再取此行 |

这是支持重试的幂等物化，不是保证模型调用、计费或所有外部动作“恰好一次”。

## 14. Rebuild、验收和线上激活

### 14.1 rebuild 读取已持久化 Child

```text
只读 Milvus 归属预检
    ↓
PG 绑定 REBUILD 目标并提交
    ↓
若已验收：重新校验已有凭据，不重写冻结目标
    ↓ 否则
统计来源数量
    ↓
游标读取 PENDING/READY Child
    ↓
按批次 Embedding、upsert、flush
    ↓
检查结束数量、完整验收
```

不重新运行 ParentChildIndexBuildStrategy，不重新切原文，不修改原 Child 的 ingestion 状态。
一般使用新的空物理 collection，或重试同一个未验收的中断构建。
仅靠 upsert 不会清除目标中旧的多余记录，因此最终验收仍需检查数量和 payload。

### 14.2 写保护

`collection_write_guard()` 取得 collection 物化事务锁，然后检查是否已有成功验收。
已验收的物理目标不再允许正常写入，避免线上 snapshot 被原地污染。
这依赖正常写入路径遵守 guard，不是 Milvus 自己天然只读。

### 14.3 只允许 REBUILD 验收

`validate_serving_collection()`：

1. profile 来源独占锁。
2. collection 物化锁。
3. 检查 PG binding 存在、profile 一致、purpose 为 REBUILD。
4. 检查 Milvus 物理目标存在且 owner 合法。
5. Strong 读取数量，再按 ID 分批读取业务 payload。
6. 与 PG 逐字段比较，检查缺失、多余、重复或不一致。
7. 保存成功验收，失败不写成功记录。

不允许 INGESTION 验收，避免冻结该 profile 唯一的持续写入目标。

### 14.4 两种摘要不要混淆

| 摘要 | 范围 | 用途 |
|---|---|---|
| `prepared_digest` | 单文档完整准备结果，含 Parent/Child/Link 和顺序 | staging 幂等和不可变比较 |
| `authoritative_snapshot_digest` | 某 profile 排序后的全部 Child 业务 payload | collection 验收和激活来源复核 |

collection 摘要由 profile 和按 chunk_id 顺序排列的规范 payload 逐条计算，排除 PENDING/READY、时间和 embedding 向量。
状态变化不应使同一业务正文集合变成另一份来源快照。
当前 collection 摘要不显式覆盖 Parent/Link，这是已记录的加强项，不要误以为 prepared_digest 已替代此绑定。

### 14.5 激活是单独操作

`activate_serving_collection()` 的关键步骤：

```text
检查标识和 expected_route_version
    ↓
找到成功验收凭据
    ↓
授权回调必须显式返回 True
    ↓
激活请求键锁 + profile 来源独占锁
    ↓
再次确认绑定为同 profile 的 REBUILD
    ↓
同幂等键已有一致回执？返回原 snapshot
    ↓ 否则
检查当前路由版本等于预期版本
    ↓
复核 PG 来源 count/digest 没有变化
    ↓
物化锁 + 再次验证 Milvus payload
    ↓
写 Activation 历史，更新 Route，调用方同事务提交
```

版本推进：

$$
v_{new}=v_{expected}+1
$$

未配置 route 时视作当前版本零，首次成功激活生成版本一。
不同操作者、build 或预期版本重复使用同一幂等键，会失败。
一致重试返回原激活版本，即使今天当前 Route 已经前进，也不再次切换或回退 Route。

### 14.6 查询路由

`resolve_serving_route()` 按 profile 读取当前指针，返回：

```python
RetrievalRouteSnapshot(index_profile=..., collection=..., route_version=...)
```

缺失时明确失败，不回退 `riskmind_knowledge_active`。
一次检索只 resolve 一次，后续沿用 snapshot，避免同请求混用不同线上版本。

## 15. 完整生命周期示例

```text
文档 A / v1
    ↓ stage_prepared
PG 新增 Child，状态 PENDING
    ↓ ingest 到 ingestion_A
Child READY

rebuild 到 rebuild_v1
    ↓ 验收
    ↓ 显式激活
route version=1

文档 B / v1
    ↓ stage_prepared
    ↓ 仍 ingest 到 ingestion_A
文档 B Child READY
route 仍指向 rebuild_v1

rebuild 到 rebuild_v2，包含 A+B
    ↓ 验收
    ↓ expected_route_version=1 激活
route version=2
```

这说明：

```text
权威知识新增 ≠ 线上快照自动改变
READY ≠ serving acceptance
验收成功 ≠ 自动激活
```

## 16. 迁移和数据库约束

迁移文件：

- [`0001`](../backend/alembic/versions/20261017_0001_create_knowledge_chunks.py)：早期 Child 表。
- [`0002`](../backend/alembic/versions/20261024_0002_index_profiles.py)：profile 隔离、联合主键及 Document/Build/Parent/Link/Collection。
- [`0003`](../backend/alembic/versions/20261025_0003_serving_routes.py)：collection 验收、激活历史与当前路由。

修改 Python 模型不等于修改实际数据库，必须评估并执行对应迁移。
0002 兼容旧数据，设置 Baseline profile，保留旧 Chunk ID、正文和入库状态。
0003 的 downgrade 在存在验收或激活历史时拒绝直接丢弃这些记录。

数据库约束是第二道防线，不代替业务逻辑：

| 规则 | 主要保护方式 |
|---|---|
| 组合身份不重复 | 主键/唯一约束 |
| profile 取值合法 | CHECK，部分表还通过 FK 间接约束 |
| 父子目标存在且 profile 相同 | 复合 FK |
| 同父内序号不重复 | 唯一约束 |
| 每个 Child 必须有完整 Link | 构建契约与业务检查，不只靠 FK |
| 同一文档版本不被改写 | 原文比较、摘要、仓储约定 |
| collection 已通过完整校验 | serving 逐字段核验与验收流程 |
| 只有已授权人员能激活 | 授权回调与服务逻辑 |
| 历史只追加、验收不可变 | 正常应用路径约束，不能误称所有表均有禁止 UPDATE 的触发器 |

## 17. 实现边界与常见误解

1. **加锁不等于数据库内容不会被人工改写。** advisory lock 需要参与者遵守约定。
2. **有摘要不等于每次重试全量复核已存储数据。** staging 的已有 Build 快路径不会修复实际行。
3. **分页不等于来源固定快照。** 完整 rebuild 的最终验收仍是必要步骤。
4. **count 相同不等于内容相同。** 因此验收比较完整业务字段。
5. **PG rollback 不撤销 Milvus。** 跨系统恢复依赖有序写入与幂等 upsert。
6. **READINESS 不是上线。** PENDING/READY 只表达 ingestion 处理进度。
7. **索引不是业务校验。** 普通 Index 主要改善查询，唯一与 CHECK/FK 有各自边界。
8. **profile 不等于风控 model_id。** 它表达索引方案，不是报告所讨论的模型。
9. **本轮文档不改变已有支持边界。** Parent/Link 未绑定 serving digest、长时间验收持独占锁、preflight 后极窄竞争窗口、legacy Baseline adoption 等已知事项仍需后续工程评估。

## 18. 可运行的离线示例

以下两个示例不需要数据库连接，不修改业务数据。

### 18.1 规范 JSON 保留列表顺序

```python
from pydantic import BaseModel
from risk_platform.knowledge.repository import _canonical

class Example(BaseModel):
    title: str
    ids: list[str]

first = Example(title="调查报告", ids=["C1", "C2"])
same = Example.model_validate({"ids": ["C1", "C2"], "title": "调查报告"})
reordered = Example(title="调查报告", ids=["C2", "C1"])

assert _canonical(first) == _canonical(same)
assert _canonical(first) != _canonical(reordered)
print(_canonical(first))
```

输出：

```json
{"ids":["C1","C2"],"title":"调查报告"}
```

### 18.2 编译查询，观察 ORDER BY 和游标条件

```python
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from risk_platform.knowledge.models import KnowledgeChunkRow

statement = (
    select(KnowledgeChunkRow.chunk_id)
    .where(
        KnowledgeChunkRow.index_profile == "baseline-v1",
        KnowledgeChunkRow.ingestion_status.in_(("PENDING", "READY")),
        KnowledgeChunkRow.chunk_id > "chunk-002",
    )
    .order_by(KnowledgeChunkRow.chunk_id)
    .limit(2)
)

compiled = str(statement.compile(
    dialect=postgresql.dialect(),
    compile_kwargs={"literal_binds": True},
))
assert "ORDER BY knowledge_chunks.chunk_id" in compiled
assert "LIMIT 2" in compiled
assert "knowledge_chunks.chunk_id > 'chunk-002'" in compiled
print(compiled)
```

这里只编译示例 SQL，不执行。`literal_binds` 用于展示固定测试值，生产查询继续使用绑定参数，不能照搬字符串拼接用户输入。

## 19. 测试与复习清单

相关测试入口：

| 文件 | 关注点 |
|---|---|
| [`test_models.py`](../backend/tests/knowledge/test_models.py) | 模型结构与初始约束 |
| [`test_repository.py`](../backend/tests/knowledge/test_repository.py) | Child staging、重复与冲突、状态更新 |
| [`test_profile_persistence.py`](../backend/tests/knowledge/test_profile_persistence.py) | 两种 profile 的权威持久化 |
| [`test_repository_postgres.py`](../backend/tests/knowledge/test_repository_postgres.py) | 真实 PG 场景 |
| [`test_profile_postgres.py`](../backend/tests/knowledge/test_profile_postgres.py) | 真实迁移、并发 staging 和归属竞争 |
| [`test_serving.py`](../backend/tests/knowledge/test_serving.py) | 验收、幂等激活和生命周期 |
| [`test_serving_postgres.py`](../backend/tests/knowledge/test_serving_postgres.py) | 真实 PG serving 并发与约束 |

SQLite 测试可以验证部分 SQL/FK 和业务逻辑，但不能证明 PostgreSQL advisory lock 的真实行为。
是否运行了真实存储集成，应以具体运行环境和实际测试报告为准，不因测试文件存在就宣称通过。

### 自测

1. 为什么 staging 不在仓储内部 commit？
2. 为什么原文身份不包含 profile，但构建身份包含？
3. `_canonical()` 是否会排序 Child 列表？
4. 为什么有 Chunk ID 还要有 prepared_digest？
5. 文档锁和 profile 来源锁分别保护什么？
6. `flush()`、函数 return、事务 COMMIT 哪个释放事务级锁？
7. 已 READY 的 Child 再次 staging，为什么不冲突也不退回 PENDING？
8. `order_by(chunk_id)` 是否代表文档顺序？
9. 为何 rebuild 读取 PENDING 和 READY？
10. Milvus 已写成功、PG 未标 READY 时如何重试？
11. 为什么不能验收 INGESTION target？
12. route 切换与新增权威知识是不是同一件事？

### 对照答案

1. 多表写入需由调用方同事务完成，失败整体回滚。
2. 两种策略共享同一文档版本原文，各自拥有独立构建产物。
3. 不会，字典键排序但列表顺序保留。
4. ID 定位身份，完整摘要比较整个准备结果与顺序。
5. 前者协调同文档版本写入，后者协调来源集合变化与验收/激活。
6. 正常提交或回滚结束事务时释放，不是 flush 或普通 return。
7. 比较业务内容而非处理状态，一致重试不覆盖已有记录。
8. 不是，只是稳定的数据库 ID 排序。
9. 重建的是完整权威集合，不只处理未完成的增量任务。
10. 保持 PENDING，重试相同 ID 的 upsert，再标 READY。
11. 验收会冻结目标，INGESTION 必须持续可写且当前不能任意轮换。
12. 不是；只有 Rebuild → Validate → Activate 才推进线上快照。
