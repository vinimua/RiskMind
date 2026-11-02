# Knowledge 文档管理：增删改查、版本生效与发布设计

状态：待 review 的实施设计，尚未新增数据库表、管理 API 或改变既有 RAG 行为。

## 1. 设计目标

提供可以实际管理的文档生命周期，同时保留现有不变量：

- document_id + document_version 对应的原文及构建结果不可原地覆盖。
- PostgreSQL 是权威存储，Milvus 是可重建搜索副本。
- Baseline 和 Parent-Child 保持 profile 隔离，切分和 ID 规则不改。
- INGESTION 持续增量物化，不自动成为线上服务。
- REBUILD → Validate → Activate 是线上集合变更路径。
- 删除或更新知识不能破坏已经发生的调查引用。

CRUD 的对象是“逻辑文档及版本”，不能只提供一个删除 Child 表记录的接口。

## 2. 当前代码与缺口

| 能力 | 已实现 | 本设计补充 |
|---|---|---|
| 保存不可变文档版本 | stage_prepared 支持 | 管理查询、可信上传入口及操作审计 |
| 两种 profile 构建 | 已支持 | 检查发布选中的版本是否具备对应构建 |
| 增量入库 | PENDING→READY | 维持原语义，不把它当作上线开关 |
| 完整 rebuild | 当前读取 profile 全部 Child | 增加明确、持久化的来源选择清单 |
| 文档更新 | 新版本可以共存 | 显式选择后续发布版本，旧版退出新快照 |
| 文档删除 | 无完整生命周期 | 默认撤回，保留历史；物理清除单独设计 |
| 管理查询 | 无完整 CRUD API | 列表、详情、版本、构建、各 profile 线上状态 |

尤其重要：现有 count_chunks/list_rebuild_chunks/authoritative_snapshot/验收都围绕“全部 Child”。不能只修改其中一个查询来过滤旧版本，否则数量、摘要、Milvus 与激活语义会相互冲突。

## 3. 三层状态必须分开

```text
不可变知识存档：原文 + 版本 + profile 构建产物
    ↓ 管理员选择
期望发布目录：下一份快照应包含哪些文档版本
    ↓ 固定为 manifest，重建、验收、显式激活
实际线上状态：每个 profile 当前 route 指向的 collection
```

例如：

```text
存档：A/v1、A/v2、B/v1
期望目录：A/v2、B/v1
Baseline 线上：仍为旧快照 A/v1、B/v1
Parent-Child 线上：可能已切为 A/v2、B/v1
```

这是允许的过渡状态。管理端必须分别展示，不能把“已选 v2”写成“所有策略已上线 v2”。

## 4. 术语约定

| 术语 | 语义 |
|---|---|
| registered version | 原文版本已进入不可变存档 |
| selected version | 管理员选定后续构建使用的版本 |
| withdrawn document | 后续目录不再选择此文档；历史原文仍保留 |
| manifest | 某次 profile 构建的不可变来源清单 |
| accepted collection | 已按该 manifest 验收的物理集合 |
| serving version | 当前 route 所指 collection 的 manifest 实际包含的文档版本 |

不增加含糊的通用 ACTIVE 字段，同时表达“选中、验收、线上”的三种不同概念。

## 5. 新增文档

### 5.1 注册与构建

1. 应用认证操作者，执行知识写入权限检查。
2. 校验 KnowledgeDocument，确定稳定 document_id 与显式 document_version。
3. 执行已有 strategy.build 与 stage_prepared；保存原文及对应 profile 构建。
4. 同 ID/version 相同内容重复请求为幂等；不同内容必须冲突，不能偷偷覆盖。
5. 返回注册和构建情况，不声称已经上线。

管理端首版可以接受既有 KnowledgeDocument JSON；Markdown 文件解析可复用现有能力。PDF/DOCX/OCR 作为独立解析适配，不能为了 CRUD 顺带实现全部格式。

### 5.2 选择与发布

注册成功后，显式把该版本加入期望目录，再创建 profile manifest、rebuild、validate、activate。

不自动为所有上传内容选择版本，避免未完成 review 的原文立刻进入下一次发布。

## 6. 修改文档

正文、标题、来源 URI、页码等 canonical 字段变化，统一创建新版本。

```text
A/v1 保留
    ↓ 注册 A/v2、构建所需 profile
选择 A/v2 作为后续来源
    ↓
新 manifest 只选 A/v2，不选 A/v1
    ↓
新集合重建、验收、激活
```

不按字符串排序自动选“最新版本”：v10、v2、日期标签都可能有不同含义。生效由显式选择决定。

若一个目标 profile 尚无 A/v2 的完整构建，该 profile 发布必须失败，不能回退 A/v1，不能静默漏掉 A，也不能跨 profile 复用 Child。

## 7. 删除文档：默认解释为撤回

### 7.1 从后续搜索快照撤回

1. 认证和授权。
2. 检查预期目录 revision，防止并发覆盖。
3. 将逻辑文档设为 withdrawn，并移除其期望版本选择。
4. 同一 PG 事务内记录操作凭据、操作者、原因与前后值。
5. 为需要更新的 profile 构建不含该文档的新 manifest。
6. 重建、验收、激活后，新开始的该 profile 请求不再命中它。

原文、旧 Child/Parent/Link、旧验收记录暂不删除。撤回没有改变旧集合，因此不能承诺请求一点击删除就停止访问。

### 7.2 管理端必须返回的状态

```text
desired_state = WITHDRAWN
baseline_serving_contains_document = true/false
parent_child_serving_contains_document = true/false
```

如果任一 profile 仍在旧快照，展示“撤回待发布”，不能返回“线上已彻底删除”。

### 7.3 物理清除另行处理

物理清除涉及历史引用、调查记录、激活历史、快照、备份与保留政策，不属于首版 DELETE 行为。

清理前必须确认没有受保留要求约束的引用、当前 route 或仍在执行请求需要的数据，并定义可审计的清理记录与依赖顺序。不能直接 CASCADE 删除整个来源历史。

敏感知识要求即时阻断时，应另设撤权/禁用访问机制。它需要覆盖旧 snapshot、运行中请求和权限边界，不能用普通 rebuild 延迟发布来承诺即时删除。

## 8. 恢复与回滚

撤回后可显式重新选择一个保留的不可变版本；如需恢复旧内容，可重新选择 A/v1。每次选择都产生新 revision 与审计记录。

首版通过新的 manifest/集合发布恢复，不承诺直接把 route 指回任意旧集合即可完成恢复。旧验收的来源检查与目录 revision 可能已经失效，不能绕过它们。

日后如需快速 route rollback，应单独定义旧 build 可重新激活的来源资格和授权规则。

## 9. 查询：管理查询与语义搜索分开

| 查询 | 数据源与语义 |
|---|---|
| 文档列表 | PG 逻辑文档目录，分页、过滤 |
| 某版本详情 | PG canonical 文档，指定 document_id/version |
| 版本列表 | 该文档已注册的版本，不按名字猜最新版 |
| 构建详情 | 每个 profile 是否有完整构建、数量与构建摘要 |
| 期望状态 | 目录选择的版本或 withdrawn |
| 实际服务状态 | 各 profile route→collection→manifest 中的版本 |
| 语义搜索 | 继续使用现有 serving snapshot，不自动搜索存档 |

读取原文也必须鉴权。知道 document_id 或 source_uri 不是访问许可。

语义过滤 document_version 只在当前物理集合的内容中生效。指定一个仅存档、未发布的版本，不会自动从 PG 构建临时向量结果。

## 10. 建议的最小新增持久化概念

以下是概念设计，最终字段、迁移号及命名需实施前 review。

### 10.1 逻辑文档管理记录

```text
document_id（唯一）
selected_version（可为空；复合 FK 指向 canonical document）
withdrawn
revision
updated_at / updated_by
```

约束：withdrawn 时不能仍选中版本；选中的版本必须属于该 document_id；更新必须使用 expected revision。

首次注册允许未选中。旧数据迁移不能随意猜一个版本生效。

### 10.2 目录 revision 与操作凭据

文档选择/撤回使用一份单调递增的 catalog_revision，供固定 manifest 和激活并发检查。文档自身 revision 用于防止同文档并发编辑冲突，两者职责不同。

写入操作凭据保存 operation_id/idempotency_key、请求摘要、操作者、动作、前后选择与 revision、时间和结果身份。相同幂等键同请求返回原结果，不同请求冲突。

存储层审计要和管理状态在同一事务提交；不能复制参考项目“审计失败只日志放行”的关键写入策略。

### 10.3 不可变来源 Manifest

```text
manifest_id
index_profile
catalog_revision
ordered (document_id, document_version) entries
ordered selected Child payload digest / count
created_at / created_by
```

原文版本条目需要 FK 或等价强约束。即使一个文档没有 Child，文档身份清单也不能只靠 Child 数量表达。

collection 构建与 acceptance 必须绑定同一 manifest。可以使用独立关联表或迁移扩展验收引用，不能只放在未受约束的自由 metadata 中。

## 11. Manifest 如何与现有 rebuild 衔接

```text
在受控 PG 事务中固定目录 revision 与版本清单
    ↓
检查每个 selected version 在目标 profile 有完整 build
    ↓
生成 immutable manifest
    ↓
rebuild 只读取 manifest 引用的 Child
    ↓
validate 对同一 manifest 做数量、ID、逐字段与摘要核验
    ↓
activate 核对权限、expected route version、catalog revision、manifest 与实际集合
```

旧的全量来源构建不能在迁移后被悄悄按“当前 selected 版本”重新解释。已有 acceptance 与 collection 仍代表原来的来源集合，保留原解释路径或明确版本化标识。

INGESTION 保持现有权威 Child 增量物化职责，可以含多个历史版本；线上只搜索显式 route 指向的集合。不要为了管理删除去原地删除 INGESTION Child 以改变 READY 语义。

## 12. 验收、摘要与并发必须统一

需要一起设计和测试的调用点：

```text
来源计数 / 分页读取
authoritative_snapshot
rebuild_collection
validate_serving_collection
activate_serving_collection
benchmark 的语料核验
```

来源选择的数学表达为：

$$
S_{p,m}=\{c\mid profile(c)=p\land(docID(c),version(c))\in Entries(m)\}
$$

Manifest 摘要应覆盖版本清单、profile、规范化 Child 载荷及其确定顺序。不能只记录 count，也不能只给查询追加 selected=true 而让其他调用仍统计全部 Child。

Parent/Link 继续保持 canonical 不可变；是否将它们纳入新 manifest 摘要需单独 review，不能声称现有 acceptance 已经覆盖此 P2。

## 13. 并发示例

```text
构建 M1 时目录 revision=10，选择 A/v1
    ↓
管理员选择 A/v2，目录 revision=11
    ↓
M1 仍可作为旧来源完成物化和校验
    ↓
激活 M1 时发现目录已经变化，拒绝上线旧选择
```

长时间 Embedding 不持有整个目录的独占事务；manifest 固定来源，激活阶段再次核对 revision。

首版采用保守规则：目录 revision 变化就要求重新准备候选，不做“虽然 revision 变了但最终选择恰好相同”的自动推断。

已有请求沿用旧 snapshot，保留旧数据。新查询在激活后读取新 route；如果即时撤权政策要求不同，走独立访问阻断设计。

## 14. 建议 API：这是目标接口，不是已有能力

| 操作 | 建议入口 | 成功语义 |
|---|---|---|
| 注册版本 | POST /knowledge/documents/{id}/versions | 已保存不可变原文及明确构建结果，不等于发布 |
| 列文档 | GET /knowledge/documents | 管理目录分页 |
| 列版本 | GET /knowledge/documents/{id}/versions | 指定逻辑文档的存档版本 |
| 读原文 | GET /knowledge/documents/{id}/versions/{version} | 已鉴权的 canonical 详情 |
| 查切片 | GET /knowledge/documents/{id}/versions/{version}/chunks?index_profile=... | 指定 profile 的存储产物 |
| 选择版本 | PUT /knowledge/documents/{id}/selection | 更新后续发布选择，需 expected revision |
| 撤回 | DELETE /knowledge/documents/{id} | 逻辑撤回，明确尚需发布；不物理删除 |
| 服务状态 | GET /knowledge/documents/{id}/serving | 分 profile 展示实际版本与待发布差异 |

POST/PUT/DELETE 接受可信操作者身份、幂等键与必要的版本前置条件，不能由 LLM 自称管理员。

知识管理动作和发布动作分开，上传接口不自动给操作者 serving activation 权限。冲突时返回明确冲突错误，不 last-write-wins 覆盖。

## 15. 构建失败和部分成功

同一个文档版本可在两个 profile 分别构建；其中一个失败不能把另一个 profile 的已成功存档说成不存在，也不能把失败 profile 标为 ready。

首版先提供确定性业务函数，显式返回每个 profile 的实际构建结果；是否一请求同时构建两种策略需单独定义原子性。大文件后台化交由后续既定任务设施，不新增第二套工作流。

幂等注册与构建需复用已有 stage_prepared 的内容冲突检查；请求幂等键用于关联操作收据，不能代替原文/构建摘要校验。

## 16. 迁移边界

- 不删除或原地重写现有 canonical、Child、Parent、Link。
- 明确保留旧 collection/acceptance 的全量来源语义。
- 旧文档若只有一个版本，可由迁移计划显式登记其选择；存在多个版本时输出冲突清单，由操作者选择，禁止凭字符串排序自动决定。
- 旧 stage_chunks 可能只有 Child 而没有 canonical/build；迁移先识别并补齐可验证来源，无法证明则阻止进入新 manifest。
- 新 catalog、manifest 与验收关联约束需真实 PostgreSQL 迁移测试，不用 SQLite 或 SQL 生成代替。
- 回退迁移若会丢失选择/发布语义，应显式拒绝有数据的降级，而非静默丢表。

## 17. 最小实施分步

### D1：文档管理契约与存档查询

先定义管理读模型与不可变版本注册服务，复用现有 canonical/staging。读接口区分 stored、selected、serving，不构造不存在的 active 状态。

不接 selected-only rebuild，不修改封板切分与查询。该阶段不能宣称“编辑后旧版已替换”或“删除已退出搜索”。

### D2：目录选择、撤回与不可变 Manifest

迁移与业务函数一起落地：expected revision、幂等操作凭据、来源 FK、profile 构建完整性。此时只改变下一次候选来源，仍不自动变更线上路由。

### D3：Manifest-aware rebuild / validate / activate

统一所有来源计数、分页、摘要及验收/激活路径；保留旧构建语义。完成真实 PG+Milvus 跨存储生命周期测试，才开放“撤回发布完成”的用户状态。

### D4：管理 API 与 UI

调用已验证业务函数，提供预览、版本差异、状态、失败重试和发布差异展示；不把权限或版本选择逻辑放前端。

常规物理清理和即时撤权另立范围，不混入 D1-D4。

## 18. 必须通过的验收场景

1. 注册 A/v1 两次：不重复生成不同知识记录；同身份异内容拒绝。
2. A/v2 注册后，A/v1 原文和历史引用保持不变。
3. 选择 A/v2 但不发布：当前 serving 仍为 A/v1，页面明确待发布。
4. 新 manifest 只含 A/v2，重建验收后切换 route，默认新检索不再看到 A/v1。
5. 撤回 A，不发布时旧 serving 仍含 A；发布后不再含 A；历史可授权追溯。
6. 目标 profile 缺少选定版本的 build：整次候选准备失败，不漏文档、不回退旧版。
7. 同文档并发选择：一个成功，一个 revision 冲突；幂等重放返回原收据。
8. 物化期间目录更新：旧 manifest 不漂移，激活旧 revision 失败。
9. 验收缺失/多余/载荷损坏：不能激活，旧服务保持可用。
10. 空目录构建：得到真实空集合，验收并激活后新查询为空。
11. 两种 profile 的发布进度可以不同，服务状态按实际 route 分别展示。
12. 未授权新增、读取、选择、撤回、发布：拒绝且无外部写入。
13. 原有全量构建验收不会被新目录语义重新解释。
14. 本来不可物理删除的历史引用不能被 CRUD 接口绕过。

## 19. 从参考项目借鉴与不照搬的内容

DevF 可借鉴来源管理、上传状态、切片查看、按来源识别关联片段和失败重试；python 可借鉴预览、指纹查重、pending 补偿和审核入口。

不照搬“删除旧片段再写新片段”到已验收集合，不让自增 ID 替代不可变版本，也不把跨 PG/Milvus 的两个调用当作一个事务。

内容相同但来源不同可以是两个合法文档；哈希查重可以提醒，不应未定义业务规则就全局去掉不同来源。

## 20. 当前项目顺序

本设计不宣布文档 CRUD 已实现，也不自动替换已经确认的 Stage 7B 路线。
建议先 review 本设计，再明确 D1-D3 与 Investigation Execution 的排期；正式支持旧版本替代/撤回上线时，必须完成 D3，不能只交付 CRUD 按钮。

## 21. 对照源码

- [当前文档与构建表](../backend/src/risk_platform/knowledge/models.py)
- [不可变 staging](../backend/src/risk_platform/knowledge/repository.py)
- [增量与重建](../backend/src/risk_platform/knowledge/ingestion.py)
- [验收与路由激活](../backend/src/risk_platform/knowledge/serving.py)
- [检索和来源校验](../backend/src/risk_platform/knowledge/retrieval.py)
- [当前入库指南](knowledge-ingestion-guide.md)

参考项目保持本地使用，不建立仓库文档对被忽略目录的必需依赖。

## 22. 补充讨论：跨 PG、Milvus 与文件存储的删除一致性

本节是后续物理清理/即时禁读的设计补充，不表示当前已实现删除任务、访问阻断或后台清理，也不把这些能力并入 D1-D4。

### 22.1 为什么严格报错仍不等于原子删除

DevF 会在删除 Milvus 失败时向上传播异常，但 PG 事务不能撤销已经完成的 Milvus 删除或文件删除。

```text
Milvus 删除成功
    ↓
文件删除失败，或 PG commit 失败
    ↓
PG 可以回滚自己的事务，但不能自动恢复向量或原文件
```

因此“所有操作写在一个函数里”或“最后统一 db.commit()”，都不能证明多个存储一起成功或一起回滚。

首选方式是可靠登记删除意图、明确访问生效边界、幂等执行清理，最终对账；不是假设存在跨三种存储的事务。

### 22.2 两种删除目标继续分开

| 目标 | 生效方式 |
|---|---|
| 普通知识撤回 | 目录排除来源，构建并激活新 serving snapshot |
| 紧急禁读 | 独立可信访问阻断机制，不能等待普通重建才生效 |
| 物理清除 | 在保留与引用约束允许后，异步清理约定存储范围 |

物理清除通常不可逆，不能把“重试清理”和“恢复旧文档”当成同一种操作。普通撤回可以保留历史供恢复，物理清除开始后是否允许恢复必须由策略明确限制。

### 22.3 第一步：同一 PG 事务保存状态、清理任务与审计

```text
检查权限、预期版本、保留约束和幂等键
    ↓
登记文档删除/清理意图
登记带 operation_id 的持久化清理任务
记录操作者、原因、清理范围和来源定位信息
    ↓
同一 PG 事务提交
```

删除意图和持久化任务必须一起提交。不能先删外部文件、再尝试登记任务，否则进程中断后可能失去追踪依据。

任务至少保留准确的 document_id/version、待处理集合或向量身份、原文件定位，以及各步骤进度；即使准备最终删除来源记录，也不能提前删除唯一的清理依据。

上文 DELETING/DELETED 一类名称仅为候选清理状态，不替代目录 withdrawn、各 profile 发布状态或案件状态。

### 22.4 第二步：可靠交付后台执行，不丢失已提交任务

PG 提交成功后再发队列消息，也存在“提交后、发送前崩溃”的窗口。后续应采用事务内 outbox 或可重复扫描的 PG 待处理任务表，让 worker 能重新领取任务。

重复消息按 operation_id 关联同一删除操作；使用领取租约或条件更新协调多个 worker，防止并发清理与进度互相覆盖。

Celery 只执行耗时工作，PG 保存任务和清理事实；LangGraph 仍是既定业务流程编排器，不额外创建第二套案件状态机。

### 22.5 第三步：查询阻断需要覆盖所有实际入口

写入 PG 的删除标记不会自动改变 Milvus 查询。若要求立即禁读，需要让可信查询路径实施阻断，包括：

- 向量候选与关键词候选；
- PG 原文、Child 与 Parent 读取；
- 缓存、旧 route snapshot 和历史 Context 再取用；
- 已检索结果交给生成模型之前的数据交接。

可以选择同步索引标记与查询过滤，或按权威阻断状态复核；都需要定义同步延迟、不可用时行为，以及候选不足时是否补召回。

例如前五个结果中三个被阻断，只在返回后删除它们会剩两条，不会自动补上第六条。不能把这种后过滤描述为召回能力没有损失。

即时禁读还必须明确请求边界：已经发送给外部生成服务或用户的内容无法靠修改标记收回。保证可以定义为某个确认时点之后的新读取被阻断，但不能承诺撤销此前已泄露的数据。

该机制涉及新行为，必须独立设计和验收，不能悄悄改变当前 fail-closed retrieval 的语义。

### 22.6 第四步：逐项幂等清理并持久化进度

```text
持久化清理任务
    ├── Milvus 向量/可清理旧集合
    ├── 原文件或对象存储对象
    └── 允许清除的 PG 关联数据
```

“已不存在”可视为目标删除步骤完成，但前提是确实确认了正确目标不存在；网络超时、权限拒绝不能当作删除成功。

每个步骤需要精确限定范围，不允许空条件删除全集合。删除记录或文件时还需防止误删复用的资源：文件定位应采用受控路径或不可变对象身份，不能凭用户提供的任意路径执行删除。

典型崩溃窗口：

```text
外部向量已删除
    ↓
尚未写入“该步骤完成”就崩溃
    ↓
重新领取任务，再次删除同一身份
    ↓
目标已不存在，确认完成后推进进度
```

这属于向前恢复，不需要为每次失败重建被删向量。自动重试不能获得一个“新文档”身份，也不能改变已授权的清理范围。

### 22.7 不能逐条修改正在服务的已验收集合

RiskMind 已冻结的集合不应为了普通 CRUD 清理而原地删除 Child，否则破坏 acceptance 与 serving snapshot 的一致性。

普通撤回先发布排除目标的新集合，旧集合保留到不再被 route、运行中请求及历史保留要求使用后，再按策略清理；通常优先清理整个已退休集合，而非修改其已验收正文集合。

存在紧急物理清除要求时，需要同步设计旧快照不可再服务、访问阻断、历史引用失效说明和审计记录，不能仍将它标为原验收内容完整可用。

### 22.8 第五步：明确对外状态，最后才宣告完成

| 对外描述 | 可以承诺什么 |
|---|---|
| 删除已受理 | 意图、权限结果和清理任务已可靠保存 |
| 后续发布已撤回 | 新目录不再选择该文档 |
| 线上撤回完成 | 目标 profile 已激活不含该文档的新快照 |
| 禁读已生效 | 独立访问阻断机制已达到约定边界 |
| 物理清理进行中 | 部分存储仍可能保留数据 |
| 物理清理完成 | 已确认约定范围内全部步骤完成 |
| 需人工处理 | 某些步骤永久失败或超过重试策略 |

来源记录、任务进度与审计保留到满足策略，不能为了显示“删除成功”先把所有可追踪身份删除。

备份、对象历史版本、日志、缓存副本和已生成调查材料是否属于清除范围，必须在操作授权时明确。只删 PG、Milvus、一个本地文件，不能自动宣称所有副本都已抹除。

### 22.9 重试、告警和对账

记录尝试次数、错误类别、下次执行时间与最近结果。临时网络故障可以退避重试；权限错误、保留约束或错误配置不能无限静默重试。

定期检查已受理未完成任务、残留向量、孤立对象及元数据状态，出现不一致时告警并进入受控修复。

重复请求使用同一幂等键同载荷重放原结果，改变删除范围必须冲突或发起新的显式授权操作。

### 22.10 新增验收场景

1. PG 提交失败：不留下半个删除意图，不提前清除外部资源。
2. PG 提交后消息未发出即崩溃：outbox/扫描能重新交付任务。
3. 外部删除成功、进度未提交：重试安全，不误删其他身份。
4. Milvus 不可用或文件权限拒绝：保留进度与失败，不宣称清理完成。
5. 多 worker 领取同一任务：租约/条件更新正确，重复执行仍幂等。
6. 普通撤回不修改仍在 serving 的已验收集合。
7. 即时禁读如被实现，旧 snapshot 与直接原文读取也不能绕过。
8. 阻断过滤导致不足 top_k 时，按明确策略补召回或返回不足，不伪造命中。
9. 保留约束未满足：拒绝物理清除，历史调查引用不被破坏。
10. 清理任务重试超限：产生告警与人工处理入口。

最终保证应写为“可靠受理、明确访问生效边界、可追溯的最终一致清理”，不能写成 PG/Milvus/文件系统瞬间原子删除。
