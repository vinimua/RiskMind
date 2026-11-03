# Stage 8：持久化、恢复与幂等边界

状态：Stage 8 Design APPROVED。Stage 8A 的迁移、账本契约与 PostgreSQL 仓储已 review SEALED，P1=0。
实现与实测结果见[调查执行账本](runbooks/investigation-ledger.md)。尚未接入图节点、持久化 checkpointer 或恢复 API。
7A/7B/7C 保持封板；本阶段先处理恢复边界，不实施多轮 Replanning。

## 1. 为什么不能只加 checkpointer

当前 task_execution 在一个节点中执行整轮多个任务，节点最后才返回 State 更新。
即使图保存节点检查点，节点中途崩溃仍可能再次调用已经完成的外部服务。

```text
T0 已完成，T1 执行中进程崩溃
    ↓
图重新进入 task_execution
    ↓
若无业务执行记录，T0 的 Query/Embedding 可能再次调用
```

需要区分两类存储：

| 存储 | 保存什么 |
|---|---|
| LangGraph checkpoint | thread 的图状态、节点推进信息 |
| PG 业务执行记录 | 调查/轮次、计划、调用尝试、结果与解释的稳定身份和载荷 |

它们即使位于同一个数据库，也不能未经实现证明就假设跨连接原子提交。
PG 账本提供执行事实与幂等依据，不成为第二个流程编排器。

## 2. 小步路线

```text
8A：稳定身份、输入摘要、持久化契约与 PG 仓储
8B：单次调用的可信 runtime context、范围绑定、恢复鉴权
8C：持久化 checkpointer、节点内已完成阶段复用
8D：真实 PG 并发、重启与崩溃窗口验收
```

8A 已封板；8B 的 per-run context 与授权边界已实现，待 review。图不再永久绑定 Actor A；账本仍未接入自动恢复。

## 3. 身份分别负责什么

| 身份 | 语义 |
|---|---|
| investigation_id | 系统创建的调查身份，案件仍 OPEN |
| round_id | 本轮身份，恢复不另建新一轮 |
| plan identity/version | 已接受的不可变计划，正式 task_id 不在恢复时重新生成 |
| task_id | 正式计划中的任务身份 |
| operation_key | 某任务某阶段的逻辑工作身份 |
| attempt_id | 外部调用前登记的一次尝试 |
| execution_id | 已封板 7A 返回的执行事实身份，结果提交后稳定复用 |
| observation_id | 已封板 7C 返回的一次解释身份，不被重试原地覆盖 |

不要把逻辑任务与尝试次数混成一个 ID。外层 attempt_id 可以关联 7A execution_id，不必先重写已封板生成器。

## 4. 输入摘要与幂等规则

- 同一 operation_key + 同一 input_digest：复用已提交结果。
- 同一 key + 不同 digest：冲突，不覆盖或静默重跑。
- 摘要区分调查/轮次/任务、阶段、请求、可信范围、profile 和契约版本。
- 保存候选 query request 与 7A 实际生效 request，不能丢掉 scope 收紧后的真实参数。
- 模型调用记录模型名和 prompt/version 摘要，修改模型后的再解释不是旧结果恢复。
- 记录 schema_version，避免未来读取旧载荷时错误套用新契约。

不保存 API Key、authorize 函数、数据库连接或客户端对象。保存的 actor 身份只是审计关联，不代表永久授权。

## 5. 已完成结果与失败结果

已提交结果应保持原 execution_id、observation_id、载荷与 RetrievalRouteSnapshot，不重新调用模型或重新读取当前 route。

FAILED、REJECTED、零命中和 INCONCLUSIVE 同样是有效历史结果。恢复默认保留原事实；显式重试创建新 attempt，不能抹掉失败记录写成成功。

已接受的计划在恢复时复用，不能重走 Initial Planning 生成新 task_id，再把旧任务结果挂到新任务上。

## 6. 不确定外部调用不能承诺 exactly-once

```text
LLM / Embedding 已返回
    ↓
保存结果前进程崩溃
    ↓
PG 只有尝试记录，无法证明是否完成或已计费
```

知识读取与模型调用可在显式策略下再次尝试，但必须保留不确定的历史尝试，并承认可能重复费用。
checkpoint、租约和 UUID 都不能消除该窗口。

未来训练、审批、发布等有业务副作用的服务，必须接受稳定幂等键或提供可查询收据。不能照搬知识读取的重试策略。

## 7. 7B 节点内部的恢复粒度

需要分别保存：计划、每个任务的 Query Request、每个 Execution Result、整轮 Observation。

```text
进入节点 → 查业务记录
    ├── 结果已提交：授权读取、核对身份/摘要、复用
    ├── 尚未执行：登记尝试，再调用
    └── 执行中或不确定：按显式恢复策略处理
```

可以在 LangGraph 节点内部访问持久化记录，或受控重组节点边界，但不另建 DAG scheduler。
旧 7B 入口要求全部任务 PENDING，不能直接把已完成计划塞回去当作恢复。

## 8. checkpoint 与业务记录的协调

| 崩溃窗口 | 恢复要求 |
|---|---|
| 业务结果已提交，checkpoint 尚未保存 | 重入节点时复用业务记录，避免再次调用 |
| checkpoint 已推进，但业务记录缺失/损坏 | 完整性失败，不以图状态代替业务事实 |

结果应在节点返回 State 更新之前可靠提交；写入失败不能继续推进到看似完成的阶段。
8A 不承诺已经完成这套图集成。

## 9. 并发和过期 worker

唯一约束阻止重复 operation 登记；短事务领取租约或条件更新协调重复恢复请求。
网络调用期间不要长期持有 PG 独占事务。

租约到期后，旧 worker 可能仍在执行。提交必须核对当前 attempt/token，阻止过期 worker 覆盖新结果；这不能撤销旧请求已经产生的计费。
无需并发 DAG，也必须测试同一恢复请求重复到达时只接受一个最终提交。

## 10. 路由变化的边界

已提交的知识结果永远保留原 snapshot。尚未提交的调用重试时，当前 7A 会重新 resolve route，可能搜索新集合。
首版必须将后者记录成新 attempt，不能宣称恢复了原来的搜索快照。

如果必须让不确定尝试也复用原 snapshot，需要另行设计持久化路由后执行搜索的接口；不能只靠外部账本推断已经解决。

## 11. 可信上下文按次提供

共享图结构与单次可信运行上下文分开。身份、authorize、客户端等不进入模型可写 State 或 checkpoint。
恢复时重新认证，并验证调用方有权读取/继续 investigation_id 与 round_id。

thread_id 只是定位键，不是访问令牌。权限撤销时，即使结果已经缓存也不能绕过读取授权。

当调查绑定明确 model_id 时，登记的调查目标、planning input 和 execution scope 必须满足相同范围约束。
scope 缺失不能默认放行全库；通用无模型问题应由可信应用明确授权其范围。

7B 的构图上下文绑定和案件 model_id/scope 绑定两项 P2，在共享图或多用户恢复入口启用前必须解决。

## 12. 8A 最小交付

1. 调查/轮次系统身份和不可变输入记录，不扩案件 OPEN 之外的生命周期。
2. 计划、请求、执行结果、Observation 的持久化载荷契约。
3. operation key/input digest 复用及冲突语义。
4. attempt 与结果身份关联，不虚构外部调用恰好一次。
5. PG FK、唯一约束、事务、不可变检查。
6. 真实 PG 迁移、重复请求与并发测试。

不顺带实现 Web API、Celery、多轮 Replanning 或全部图节点恢复。

## 13. 后续恢复验收

- 同请求重放复用调查与轮次，异内容冲突。
- 已保存计划恢复后 task_id 不变。
- T0 已完成再崩溃，恢复不重复调用 T0。
- 外部完成但未保存，保留不确定尝试并按策略创建新 attempt。
- 业务结果已保存但 checkpoint 缺失，节点复用旧记录。
- Observation 恢复保留原身份；重新解释形成新尝试。
- 重复恢复请求、租约过期与旧 worker 返回不会覆盖合法结果。
- Actor B 不能仅凭 Actor A 的 thread_id 读取或恢复。
- 调查 model_id/scope 错配在外部调用前拒绝。
- route 切换不改变已提交结果快照。
- 损坏或缺失业务记录不被当作执行成功。
- 零命中、失败和拒绝不因恢复被混淆；案件仍 OPEN。

## 14. 已有 P2 与质量结论

继续记录七项 P2：私有 scalar 依赖、缺 tool_name、scope 字段有限、构图绑定身份、案件 model_id/scope 未绑定、statement/quote 语义蕴含未验证，以及 source 是 supporting Child 而非必然承载引句的 Child。

持久化阶段应明确 tool_name/schema_version 的审计身份，但不因此引入未来所有工具的通用框架。涉及已封板契约的修改单独 review。

Query Formulation 与 Observation 自然语言语义质量仍 INCONCLUSIVE。持久化只保存实际产物，不会把它们升级为 Evidence 或当前根因结论。

## 15. 框架机制参考

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：checkpoint 保存 thread 状态；内存 saver 不跨进程重启保留。
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)：节点执行与 runtime context 机制。

框架支持这些能力不等于 RiskMind 已配置；业务幂等和权限仍需应用实现。

## 16. Review 确认的 8A 实现契约

本节是 8A 的实现与验收约束；账本已独立实现，图恢复仍未接入。
8A 只实现稳定身份、canonical input digest、持久化契约、PostgreSQL 仓储及相应测试。
不接恢复执行，不改 graph.py，不引入 Celery、API、DAG Scheduler、Replanning、Policy Gate、完整 Outbox 或通用 Tool Registry。

### 16.1 最小持久化关系

```text
Investigation → Round → Accepted Plan → Operation → Attempt → Committed Result
                       本轮 Observation 同样保留稳定身份与不可变载荷
```

计划包含正式任务身份，接受后不可原地改写；恢复不能重新规划生成替代 task_id。
数据库需落实 FK、operation 唯一性、载荷契约版本及已提交事实的不可变约束。
Observation 仍使用 7C 的历史知识边界，不因被持久化而成为 Evidence。

### 16.2 Operation 是逻辑幂等单位

operation_key 标识某调查、某轮、某任务、某阶段的逻辑工作；attempt_id 标识一次尝试；
execution_id 标识 7A 产出的执行结果。不得以 execution_id 判断是否为同一逻辑工作。

| 已有记录与本次输入 | 仓储语义 |
|---|---|
| 无 operation | 从未登记尝试，可登记新的逻辑工作 |
| 同 key、同 digest、无已提交结果 | 返回已有尝试事实，不自动解释成可重试或已失败 |
| 同 key、同 digest、有已提交结果 | 可定位并复用原结果；实际读取/恢复鉴权由后续入口处理 |
| 同 key、不同 digest | CONFLICT，不能覆盖，也不能静默新建同名工作 |

同一逻辑工作若要显式重试，必须保留先前 attempt/result；重试策略及选择哪个结果用于继续调查不由 8A 仓储自动决定。

### 16.3 Attempt 正式状态

| 状态 | 含义 |
|---|---|
| STARTED | 已登记尝试，尚未可靠提交终态；不能据此证明外部请求已发出或未发出 |
| COMMITTED | 已将本次产物可靠写入业务账本 |
| FAILED | 已知本次尝试失败，保留确定的失败事实 |
| UNCERTAIN | 无法确认外部调用是否完成或产生效果，例如调用完成后、结果提交前进程死亡 |

STARTED 可以结算为 COMMITTED、FAILED 或 UNCERTAIN；终态不能原地改成另一段历史。
8A 提供记录这些事实的契约，不自动扫描并把所有 STARTED 判定为 UNCERTAIN，更不自动重试。
后续执行协调必须先排除仍活跃的 worker，再记录不确定尝试，防止把正在运行的调用误判为失联。

特别区分两种状态：

```text
Attempt.COMMITTED
    = 结果已可靠保存

TaskExecutionResult.status = FAILED / REJECTED
    = 被保存的业务执行结果失败 / 被拒绝
```

二者可以同时成立。7A 已正常返回的失败、拒绝或零命中结果，以及 7C 的 INCONCLUSIVE，
都可以作为已提交产物保存，不能仅因其业务内容不成功就宣称账本未提交。

### 16.4 已提交产物不可变

已提交的 execution_id、observation_id、payload、原始路由快照和关联身份不能被后续尝试覆盖。
相同提交重放只能核对后返回原事实；不同身份或不同载荷必须冲突。
失败和拒绝记录也不因后续成功而消失。

数据库 invariant 与仓储事务需要共同验证这一点，不能只依赖调用方约定。
数据库高权限管理员绕过约束的防护不等于本阶段提供密码学防篡改能力。

### 16.5 Canonical input digest

输入摘要必须由有版本的契约载荷生成：先转成 JSON 值，再固定对象键顺序、采用固定分隔格式、
以 UTF-8 编码，最后计算 SHA-256。禁止使用 sha256(str(dict))。

```text
有版本的输入契约
    → canonical JSON（对象键排序，列表顺序保留，拒绝非有限数值）
    → UTF-8 bytes
    → SHA-256
    → input_digest
```

对象键插入顺序不影响摘要；列表顺序、字符串正文和实际业务值变化仍影响摘要。
不为得到相同摘要而擅自 strip、重排列表或规范化正文。

各阶段输入按其实际已知内容建模，覆盖调查/轮次/计划/任务身份、operation 类型、schema_version，
以及该阶段相关的请求、可信 scope/profile、模型和 prompt 版本。
candidate_request 与 effective_request 必须明确区分；尚未由 7A 得到的 effective_request 不能虚构成已知输入。
保存 actor 供审计关联，但凭据、连接对象和 authorize 函数不得进入序列化载荷。

### 16.6 最小验收矩阵

1. 同 key 同摘要重复登记不会产生第二份逻辑工作；不同摘要冲突。
2. 对象键顺序变化摘要不变；业务值、列表顺序或契约版本变化摘要改变。
3. STARTED、FAILED、UNCERTAIN 与 COMMITTED 可明确区分，尝试历史不会丢失。
4. 已提交结果重放保留原身份与路由快照；改写 execution_id、payload 或 Observation 被拒绝。
5. FAILED、REJECTED、零命中、INCONCLUSIVE 可以作为不可变历史产物保存。
6. 跨调查/轮次/计划/任务错误关联由契约、仓储与 FK 拒绝。
7. 真实 PostgreSQL 验证迁移、事务回滚、唯一约束和并发重复提交。

这些测试证明账本能区分事实；不宣称已经完成 LangGraph 重启恢复或外部调用 exactly-once。

## 17. Review 状态与 P2-8 / P2-9

Stage 7A / 7B / 7C / 8A 均 SEALED；Stage 8 Design APPROVED，review P1=0。
原有七项 P2 保留。

P2-8：未来是否使用 LangGraph node 内 task 作为更细粒度 checkpoint 的实现选项，留到 8C 决定。
即使采用该机制，也不能替代包含身份、尝试历史、输入摘要、载荷版本、路由快照及审计关联的业务执行账本。
8A 不因此修改既有图结构。

P2-9：OperationInput v1 的不可变业务输入与 per-run/per-attempt Runtime Context 尚未完全分离。
actor_id 或 timeout 改变会改变摘要，这是当前明确的 fail-closed 行为，不据此重开 8A。
8B 需要通过显式版本演进解决授权接续与每次尝试的实际 actor 审计，不能偷偷重解释 v1。

## 18. Stage 8B：可信运行上下文与授权边界

以下为 8B 的设计约束。对应实现已完成、待 review，见[运行上下文说明](runbooks/investigation-runtime.md)。
0004 保持原样，通过新增 0005 扩展审计和版本兼容。
本小步先解决共享图的安全调用和账本访问前置条件；将账本接入节点重放、checkpointer 和真正恢复执行留到 8C。

### 18.1 三种信息分开

| 信息 | 负责什么 | 是否属于可写 State |
|---|---|---|
| 不可变 Operation 业务输入 | 定义逻辑工作；决定输入冲突与产物可否作为同一工作复用 | 只能由可信服务构造并入账，不能信任模型回写 |
| per-run Runtime Context | 当前认证 actor、调查/轮次定位、当前 scope/profile、权限检查器、执行预算 | 否 |
| per-attempt 审计信息 | 谁发起这一次尝试，以及本次实际使用的范围与预算 | 否；在开始尝试时形成持久化事实 |

可复用 compiled graph 不能永久 partial 绑定 Actor A 的 execution_context。
图可持有可安全共享的结构与依赖，但身份、案件定位和授权必须由可信应用按次提供，不能从旧 checkpoint 恢复为当前权限。
State 中的 planning_input、task_id 或 thread_id 均不构成授权凭据。
本阶段仍保留 round_complete 与 investigation_status=OPEN，不扩展案件关闭能力。

### 18.2 按阶段定义稳定输入

不能简单对所有阶段统一“从 context 删除两个字段”。需要分别依据实际业务输入建模：

| 阶段 | 稳定输入应覆盖 | 当前身份/预算的处理 |
|---|---|---|
| Query Formulation | 已接受计划与规划来源、正式任务、模型/prompt 版本、实际传给模型的 max_top_k | actor/当前授权/timeout 不因换人而改变模型输入；max_top_k 当前会进入模型，因此属于该阶段输入 |
| Knowledge Execution | 正式任务、candidate request、确定的生效业务范围/profile、query/top_k 等实际参数 | 当前 actor 与预算重新提供；当前授权还必须允许该业务范围和请求，不能靠旧结果绕过 |
| Observation | 正式计划、按顺序关联的不可变执行结果、模型/prompt 版本 | actor/timeout 不改变历史结果内容；读取这些结果仍需当前权限 |

effective request 在实际确定前不能虚构为已知输入；8B 实现需明确其形成时点及与 candidate 的关联，
不能把外部调用后的未知信息追写进已经登记的不可变输入。
每个阶段仍使用有版本契约、canonical JSON 和摘要校验。

### 18.3 调查、模型与 scope 的确定性绑定

```text
持久化 Investigation 的 planning_input
    → 确定调查目标 model_id
    → 核对当前受信任 scope
    → 核对本次图输入/正式计划归属
    → 鉴权
    → 才能进入模型或知识工具
```

MonitoringAlert.model_id 或 UserQuery.model_id 存在时，执行范围必须确定性绑定到同一目标。
scope.model_id 缺失不能被解释成允许全库；v3/v4 错配在任何外部调用前拒绝。
没有 model_id 的通用调查必须由可信应用明确授权其范围，不能由 query 文本声明“允许全库”。
调查/轮次/计划关联仍以账本为准，不能因为运行时传了另一个 ID 就改变原计划归属。
8B 不顺带设计 tenant/time window 新字段；当前 scope 无法表达的隔离不能声称已经支持。

### 18.4 读取与继续执行分别鉴权

明确区分应用动作：读取调查结果与继续调查执行。
每次读取缓存产物、重新执行或未来恢复，都必须根据当前 actor 与调查资源重新检查权限，显式 True 才放行。
授权异常、权限撤销、调查归属或 scope 错配均 fail-closed，不能回退使用旧授权或切换 profile/collection。

```text
Actor B 提供 investigation/round 定位
    → 当前认证与资源权限检查
    → 验证当前允许范围覆盖所请求的历史结果
    → 读取原始已提交产物 / 获准继续
```

授权所需最小资源元数据可由可信仓储查询；完整 Context、Observation 和来源内容不得在授权前交给模型或调用方。
允许读取不自动等于允许发起新的外部调用。
当前 scope 若不能覆盖原结果，不允许静默删掉部分命中后冒充原结果复用成功。
账本仓储仍只保存事实，不成为权限策略引擎或第二个业务编排器。

### 18.5 创作者与尝试发起人分别审计

Operation 记录谁最初定义逻辑工作；每个新 Attempt 记录本次 authenticated actor。
Actor B 获准接续后，不能因为 Operation 创作者是 A，就把 B 的新 Attempt 仍记为 A。
运行时超时预算、当前授权决策及实际执行范围应与该次尝试关联，不覆盖旧尝试的审计事实。

既有 8A Attempt 没有独立 actor 字段，因此不能把 Operation.context.actor_id 当作已被证明的 Attempt actor 回填。
兼容读取需明确标记旧记录缺少尝试级归因；新写入路径必须提供可信尝试身份。
具体新增字段和约束通过新迁移实现，不改写 0004 或破坏原结果身份、载荷及快照。

### 18.6 旧 v1 的兼容不能偷换摘要语义

不能原地去掉 v1 actor/timeout 后重算 input_digest，也不能给旧记录套用新 schema_version。
新契约必须显式区分版本，并处理同 operation_key 已存在 v1 记录的情况，避免 v2 重登记必然冲突。

兼容读取先按 v1 规则验证原始完整 payload 与 digest，再执行当前权限及稳定业务输入校验。
Actor B 的当前 context 不得直接替换旧 payload 后送给 v1 register_operation/inspect_operation。
是否提供 legacy 接续适配器、哪些旧记录必须拒绝接续，在实现前明确并测试；不能默认“忽略 actor 即可复用”。
即使允许继续，也保持原 attempt/execution/observation ID 和路由快照，新的尝试另记身份。

### 18.7 8B 验收与排除项

必须验证同一 compiled graph 顺序及并发处理两个 actor/调查时，身份、权限和 scope 不串用。
同一调查中 A 首次执行、B 获准读取/继续时，不产生仅由换人造成的业务输入冲突，且新 Attempt 归因 B。
未授权 B、已撤权 A、scope/model 错配、缺少可信 runtime context 均在模型/工具调用前失败。
v1 记录摘要与产物不变；兼容缺口显式报错，不伪造旧 Attempt actor；真实 PG 验证迁移与审计约束。

暂不实现持久化 checkpointer、节点断点恢复、自动重试、租约接管、resume Web API、多轮 Replanning 或 Celery。
8B 为这些后续能力提供安全边界，不宣称现有账本已经接入自动恢复。
