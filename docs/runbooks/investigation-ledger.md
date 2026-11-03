# Stage 8A：调查执行账本

状态：已实现，待 review。8A 没有接入 LangGraph 节点、checkpoint、恢复入口、自动重试或 Web API。
7A/7B/7C 的执行方式保持原样，因此现有图运行不会自动写入本账本。

## 1. 代码与表的对应关系

| 文件 | 职责 |
|---|---|
| `backend/src/risk_platform/workflow/persistence/contracts.py` | 固定版本的输入/产物契约、逻辑身份和 canonical 摘要 |
| `backend/src/risk_platform/workflow/persistence/models.py` | SQLAlchemy 表、复合 FK、唯一索引和 CHECK |
| `backend/src/risk_platform/workflow/persistence/repository.py` | 短事务登记、尝试结算、产物提交和历史读取 |
| `backend/alembic/versions/20261103_0004_execution_ledger.py` | 冻结的 PostgreSQL DDL 与不可变/完整性触发器 |
| `backend/alembic/env.py` | 将账本模型登记到 Alembic metadata |

数据库迁移只新增账本表/函数/触发器，不改 Knowledge 表。

```text
investigation_ledger
    ↓
investigation_rounds
    ↓
investigation_plans → investigation_plan_tasks
    ↓
investigation_operations
    ↓
investigation_attempts ⇄ investigation_results
```

Round Observation 保存在 OBSERVATION 操作的不可变 Result 中，不重复建立另一套 Observation 正文表。
正式计划保持 PENDING 原样；执行后的任务状态由既有结果推导，不能回写覆盖已接受计划。
这里没有新增案件 CLOSED 等生命周期状态。

## 2. 三层身份与摘要

所有调查、轮次、计划、尝试和产物身份由可信调用方提供并跨重复请求保持稳定，仓储不会在读取时新建身份。
8A 不建立案件创建 API 或幂等键分配服务。

Operation 的逻辑身份来自：

```text
investigation_id / round_id / plan_id / task_id / operation_type
    → 固定 JSON 数组
    → SHA-256
    → operation_key
```

Observation 属于整轮计划，task_id 为 null；另外两种操作绑定正式知识任务。
改变 scope/profile、模型或请求不会生成另一份同身份工作，而会产生 input digest 冲突。

输入摘要来自完整有版本输入的 canonical JSON：

```text
typed input → model_dump(mode="json")
            → 对象键排序、固定分隔符、保留中文和正文、拒绝 NaN/Infinity
            → UTF-8 → SHA-256 → input_digest
```

列表顺序不重排，字符串不 strip。对象键插入顺序不影响摘要；正文、可信 context、请求等变化会改变摘要。
当前契约只接受 schema_version=1，未来版本必须显式演进，不能默默按 v1 解释。

计划 ID、轮次 ID 通过 FK 指向不可变输入，因此 Operation 不必重复保存整份计划或 planning input。
OperationInput 保存可信 context 的可序列化标量用于审计；不存凭据、客户端、authorize 或连接对象。

## 3. 请求与结果分别保存什么

| Operation 类型 | 必需输入 | 允许提交的产物 |
|---|---|---|
| QUERY_FORMULATION | task_id、context、模型名、prompt_version | query_request |
| KNOWLEDGE_EXECUTION | task_id、context、candidate_request | 原 TaskExecutionResult |
| OBSERVATION | context、模型名、prompt_version、按任务顺序排列的 execution_refs | 原 RoundObservation |

prompt_version 应使用能唯一定位实际提示词的版本标识或摘要，由可信调用方提供；仓储不会推断实际使用的提示词。
tool_name 固定为 knowledge_search，只在账本中记录，不修改已封板的 7A Result 契约。

成功执行的实际生效请求仍保存在 execution.payload.request，原检索/解析快照保存在原 payload。
candidate 与 effective request 分别核对，不能把未经收紧的请求冒充实际执行参数。
失败/拒绝结果遵循 7A，不会虚构不存在的 payload 或路由快照。

## 4. 正常写入与事务边界

所有函数使用调用方传入的 AsyncSession，只 flush，不替调用方 commit。

```text
事务 A：register_investigation → register_round → accept_plan → register_operation
    commit

事务 B：start_attempt
    commit                       ← 必须先落库，之后才调用外部服务

调用外部服务                    ← 不长期占用账本数据库事务

事务 C：commit_result
    同时保存 Result + Attempt.COMMITTED
    commit
```

8A 本身不调用外部服务；上述顺序是后续集成必须遵守的调用约束。
如果在同一个事务里 start_attempt 后就调用外部 API，没有提前 commit，进程死亡仍可能连尝试记录都丢失。

已知失败或不确定时，使用 settle_attempt 结算 FAILED/UNCERTAIN，并提供受控 resolution_code。
不要将包含 API Key、DSN、provider body 的异常正文放入 code。
STARTED 不会自动被扫描或过期；本阶段没有租约、后台恢复器或失联判定服务。

## 5. 状态不能混用

```text
Attempt.COMMITTED：产物已可靠保存

TaskExecutionResult.status：SUCCEEDED / FAILED / REJECTED
RoundObservation.status：SUCCEEDED / INCONCLUSIVE
```

例如权限拒绝结果可被正常保存，此时 Attempt 为 COMMITTED、Execution 为 REJECTED。
没有正式业务结果、只能确认尝试失败时，才结算 Attempt.FAILED；无法确认调用结局则为 UNCERTAIN。

一个 STARTED 只能进入一个终态。终态不可改写。
显式 retry=True 仅允许在所有旧尝试已结算后登记新的 Attempt，不会调用工具或自动选择新结果。
即使新尝试成功，旧 FAILED/UNCERTAIN/COMMITTED 事实仍保留。

## 6. 数据库如何兜底

1. 调查/轮次/计划使用复合 FK，不能把其他轮次的计划挂到当前操作。
2. task_id 必须存在于对应计划的任务表；任务 ID、能力、顺序与计划正文一致，提交时任务集合必须完整。
3. 同一计划、任务、操作类型只能有一个 Operation；同一计划只有一个 Observation Operation。
4. 相同 Operation 使用事务级 advisory lock 串行登记；同身份不同输入抛 LedgerConflictError。
5. 部分唯一索引限制每个 Operation 最多一个 STARTED Attempt。
6. Attempt 行锁序列化提交/结算；重复提交同 ID 同载荷返回原记录，不同载荷或身份冲突。
7. Attempt 与 Result 使用双向延迟复合 FK：事务提交时必须相互对应，禁止 COMMITTED 缺产物或产物关联非提交尝试。
8. 唯一约束阻止 execution_id/observation_id 被多份产物复用。
9. 数据库校验载荷 JSON、schema_version、SHA-256 与关键身份；不可变触发器禁止原地 UPDATE/DELETE 历史。
10. populated ledger 禁止 downgrade 丢失审计历史；空账本允许回退和再次升级。

延迟 FK 允许事务内部先写产物再结算尝试，但最终 commit 仍可能失败。不能仅因 flush 成功就宣称已经提交。
迁移使用 PostgreSQL 内建 sha256，不依赖 pgcrypto 扩展。迁移采用冻结 DDL，不导入未来会变化的运行时模型。
高权限管理员禁用触发器/改写数据库不在本阶段防护承诺内；这里不提供密码学防篡改或 exactly-once 外部调用。

## 7. 仓储读取语义

inspect_operation(session, expected_input) 返回：

| 返回/异常 | 含义 |
|---|---|
| None | 此逻辑 Operation 尚未登记 |
| 空 tuple | 已登记，但没有尝试 |
| 按 attempt_number 排序的历史 | 每个 Attempt 及其已提交的类型化产物；无产物时为 None |
| LedgerConflictError | 相同逻辑身份但输入不同 |
| LedgerIntegrityError | 载荷摘要/版本或提交关联损坏 |

读取时重新验证类型与摘要，返回原 execution_id、observation_id 和快照。
存在多个已提交尝试时返回全部历史，不隐式挑选“最新成功结果”。
仓储复用能力是为后续恢复提供依据，不代表当前 graph 已会自动复用。
8A 是可信应用内部仓储，不是已鉴权的公共读取接口；后续 8B 必须按当前权限检查读取与继续执行。

Observation 登记/提交校验其引用属于本计划的已提交执行结果，按正式任务顺序一一对应。
提交时还核对系统摘要与 7C finding 的执行/context/source/quote 引用。
持久化不会证明 statement 被 quote 语义蕴含，也不会把历史线索升级为当前案件根因。

## 8. 验证

```powershell
# RISK_DATABASE_URL 指向可创建临时 schema 的测试 PostgreSQL。
$env:RUN_POSTGRES_INTEGRATION = "1"
python -m pytest backend/tests/workflow/test_ledger_contracts.py backend/tests/workflow/test_ledger_postgres.py -q
python -m pytest backend/tests -q
git diff --check
```

2026-11-03 实测：focused 32 passed；backend 全量 447 passed、7 skipped。
测试使用本机隔离 PostgreSQL，并在 UUID 临时 schema 内执行 0003 → 0004；包括旧 Knowledge 数据保留、
首次/重复/冲突登记、并发首次创建和提交、四种 Attempt 状态、显式重试历史、FK/唯一性、
直接 SQL 改写拒绝、事务回滚、解释产物、跨 Session 状态刷新，以及 downgrade 防护/模型结构核对。
7 个跳过项为其他未启用的集成场景；这些数字不代表已验证真实 LLM 语义质量或图恢复。
唯一警告是现有 LangGraph/LangChain 依赖的 PendingDeprecationWarning。

## 9. 后续边界

下一轮先 review schema、触发器、事务和并发行为，不自动进入 8B/8C。
已有七项 P2 及设计 P2-8 保留；多用户恢复前需解决 per-run context 和案件 model_id/scope 绑定。
当前失败/不确定尝试结算必须由可信调用方明确决定，没有自动失联检测或自动重试。
