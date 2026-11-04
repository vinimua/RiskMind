# prepare_plan：事务、并发控制、鉴权与恢复场景详解

本文按当前代码整理，帮助逐段审阅 `prepare_plan()`。只解释已有机制，不新增功能。

## 1. 阅读入口与职责

| 文件 | 重点对象 | 作用 |
|---|---|---|
| `../backend/src/risk_platform/workflow/graph.py` | `plan_node` | 唯一图的 prepare_plan 节点 |
| `../backend/src/risk_platform/workflow/recovery.py` | `FirstRoundRecovery.ainvoke / _claim` | 入口鉴权、稳定运行身份、获取运行权 |
| 同上 | `prepare_plan / _planning_candidate` | 准备候选、正式接受、加载计划 |
| 同上 | `_authorize / _ClaimGuard / _bind_execution_run` | 权限、持锁检查、运行代次 |
| `../backend/src/risk_platform/workflow/access.py` | `_bound_context / load_runtime_context` | 加载 canonical 案件/轮次并检查归属 |
| `../backend/src/risk_platform/workflow/runtime.py` | `check_runtime` | 通用可信身份与资源授权 |
| `../backend/src/risk_platform/workflow/knowledge_tool.py` | 知识工具绑定 | 当前首轮知识流程所需的工具配置与独立授权 |
| `../backend/src/risk_platform/workflow/persistence/planning_repository.py` | `history / start / settle / validate` | 规划日志与候选校验 |
| `../backend/src/risk_platform/workflow/persistence/planning_models.py` | 三张规划表 | 身份、尝试、接受来源的数据库约束 |

文件路径从本文所在 docs 目录解析。表中的路径用来定位代码，不表示本文实现了另一套编排器。

一句话目标：为任务执行提供一份已经正式接受、可靠保存、归属正确的计划；能复用就不重新生成。

## 2. 先把五种不同检查分开

| 问题 | 机制 | 不是在做什么 |
|---|---|---|
| 当前这个人能读/继续这个案件吗？ | `_authorize()` | 不是检查模型答案质量 |
| 当前输入属于正确案件和轮次吗？ | canonical 记录比较、FK、稳定规格 | 不是只比较一个字符串 ID |
| 当前进程还独占执行这个 run 吗？ | session advisory lock 与 `check_lock()` | 不是用户权限 |
| 当前执行者的代次是否过期？ | `guard.check(session)` | 不是任务数量或模型调用次数 |
| 这份候选确实已可靠保存并合法了吗？ | COMMITTED、摘要、结构、来源关联 | 不是证明计划一定能查清根因 |

`generation == 0` 只选择流程分支，不执行鉴权。读取 `invocation.guard` 也不会自动检查锁。

## 3. 上下文、身份与代次

外部上下文 `InvestigationInvocation` 分为 runtime 和 execution。execution 由 runner 获得运行权后填入，
不是调用方自报的权限。内部 `_Invocation` 保存 context、spec、policy、guard、planning_spec、planning_claim_id。

### 3.1 guard 保存什么

```python
class _ClaimGuard:
    connection: object  # 专用持锁连接
    backend_pid: int    # 该 PostgreSQL 会话对应的后端进程 ID
    run_id: str
    generation: int
```

- `check_lock()`：检查连接未失效、后端 PID 一致且当前会话仍持有 advisory lock。
- `check(session)`：先检查锁，再 `SELECT ... FOR UPDATE` 读取正式 Run，比较 current_generation。
- 当前 check_lock 的 SQL 检查“该专用会话持有 advisory lock”，没有逐项比对精确 lock key；
  它依赖该连接只用于本次运行锁、不被其他用途复用的实现约束。

### 3.2 generation 何时变为 1、2

```python
generation = (row.current_generation or 0) + 1
row.current_generation = generation
await session.flush()
```

这是 `_bind_execution_run()` 中的应用代码，不是数据库自动递增触发器。
flush 将修改发送至数据库，但只有外围事务 commit 后才正式生效；回滚则不生效。

- 没有正式计划时，guard.generation 先设为 0，它是内存启动标记，不能等同于已存在的 Run 行。
- 第一次正式接受计划并绑定执行运行：通常从 0 增加到 1。
- 已有正式计划的后续运行，在 `_claim()` 中绑定新执行代次，例如 1 增加到 2。
- 连续调用模型、进入下一个节点不会自动递增。
- 连接断开只导致锁在数据库确认会话结束后释放，不会自动把 generation 改为 2。
- 只有新执行者成功绑定并提交，代次才更新。

特别注意：正式接受前，A 与 B 的 guard 都可能是 generation=0。
这个阶段靠运行锁和 planning_claim_id 区分执行者，不靠递增 generation 保护。
不能把正式运行中的“1→2”示例直接套在尚未接受计划的阶段。

## 4. 鉴权来源与实际调用链

```text
_authorize(session, context)
  ├── _bound_context(..., CONTINUE)
  └── _bound_context(..., READ)
```

READ 与 CONTINUE 分别显式要求 True；可读不代表可以创建新 Attempt。

`_bound_context()` 当前会先取得当前知识执行上下文，再从 PG 加载 Investigation/Round，
并比较它们与调用方上下文中的 canonical 记录是否一致。当前账本访问适配仍面向知识流程，
虽然通用 Runtime 已不再包含知识专用字段，但不能宣称整个首轮 runner 已支持其他工具。

| 信息 | 来源 |
|---|---|
| Investigation / Round 存在性、内容、归属 | PostgreSQL canonical 记录 |
| actor_id | 已认证的可信应用 |
| READ / CONTINUE 判定 | 应用提供的 authorize_resource 回调 |
| scope / profile / top_k / 工具预算 | 可信应用安装的 KnowledgeSearchTool |
| knowledge:search 权限 | 工具独立 authorize 回调，在实际工具调用时检查 |
| 运行锁、正式运行代次 | PostgreSQL 与当前持锁连接 |

prepare_plan 中 `_authorize()` 并不等于执行 knowledge:search 授权回调。
它检查资源权限、工具配置归属及 canonical 事实；后续真实知识工具调用仍需独立授权。

### 4.1 当前确实存在的重复

一次 `_bound_context()` 中，knowledge_execution 会校验传入上下文并调用资源授权；
load_runtime_context 加载记录后又调用 check_runtime。随后 READ 路径再次做一遍类似工作。
因此同一 action 的授权回调可能重复调用，类型/绑定检查也重复；session.get 是否实际发出 SQL
还受到 SQLAlchemy 当前 Session identity map 等因素影响，不能按函数调用次数推算 SQL 次数。

后续可在一个短事务内加载和核对一次 canonical 事实，再分别判断 READ 和 CONTINUE。
但不应把首次授权结果跨模型等待缓存到整个运行结束。本文只记录优化点，不修改实现。

### 4.2 重检权限为何有用，又为何不等于原子撤权

有效拦截：

```text
00 秒：入口授权通过
05 秒：管理员撤销 CONTINUE
20 秒：模型返回
21 秒：保存前重新检查，拒绝写入
```

仍然存在的窗口：

```text
20.000 秒：权限回调返回 True
20.001 秒：外部权限系统撤权
20.002 秒：本地 PG 提交
```

Python 回调在事务代码块内执行，不代表外部权限系统加入这个 PG 事务。
当前保证“在关键边界重新检查”，不保证撤权和提交全局线性一致。
若未来要求严格撤权生效顺序，需要权限版本/锁/统一事务等额外设计，当前没有实现。

## 5. 一条完整正常路径：四个写事务加最后读取

以下 T1/T2/T3/T4 是本文局部编号；此前 `_claim()` 自己也有事务，不能理解为整个 runner 仅四次事务。
复用已有候选时会跳过 T2、模型调用和 T3；已有正式执行运行时直接加载正式计划。

```text
runner 认证和获取运行锁，构造内部调用上下文
    ↓
prepare_plan：generation == 0 ?
    ├── 否 → load_accepted_plan
    └── 是
         T1：读规划历史 / 复用候选 / 旧 STARTED → UNCERTAIN
          ↓ commit
         T2：检查重试策略 / 新建 STARTED 与 actor、claim 归因
          ↓ commit
         事务外：调用模型、构造计划、确定性校验
          ↓
         T3：候选正文 + 摘要 + Attempt.COMMITTED
          ↓ commit
         T4：正式计划 + 接受来源 + 正式 Run/Claim/generation
          ↓ commit
         更新内存 guard.generation
          ↓
         load_accepted_plan：新 Session 读取正式计划
```

### 5.1 T1：处理历史

每个写事务开始时都重新鉴权、check_lock。planning.history 使用 FOR UPDATE 锁住已有尝试行。
若找到 COMMITTED，验证候选摘要和契约后复用；若找到 STARTED：

- claim_id 与当前 planning_claim_id 相同：当前尝试尚未完成，拒绝重复进入。
- claim_id 不同：在当前确实已取得运行锁的前提下，将旧尝试结算 UNCERTAIN。

FOR UPDATE 只锁查询到的已有行。空结果不等于锁住“未来可能插入的行”，首次创建竞争由运行锁和唯一约束共同约束。

### 5.2 T2：先记录 STARTED

有 UNCERTAIN 必须允许 retry_uncertain；有 FAILED 必须允许 retry_failed_attempts。
符合策略才创建新 Attempt，记录 actor、工具上下文归因、attempt_number、planning_claim_id。
不覆盖旧 Attempt。STARTED 提交成功后才允许发出模型调用。

### 5.3 事务外生成与校验

```text
nodes.initial_planning
→ InvestigationAgent.initial_plan
→ 模型返回完整 JSON
→ 构造 InvestigationPlan
→ PlanningCandidate
→ planning.validate
```

校验目标/任务目标/expected_result 非空、能力允许、任务非空、任务 ID 唯一、初始状态 PENDING 等。
案件/轮次/plan_id 从稳定 spec 绑定，不由模型决定。当前没有语义质量证明。

此处使用 asyncio.to_thread 执行同步 SDK，并用 timeout 限制等待。
超时或取消等待不保证底层线程/远端模型立即停止，也不保证不会计费。
已知结构错误在异常结算事务中标记 FAILED；其他普通异常按 UNCERTAIN 处理。
进程退出、取消，或结算时已失去权限/锁，可能留下 STARTED，交给后续恢复处理。

### 5.4 T3：候选可靠保存

candidate、candidate_digest、COMMITTED 一起提交。
此时只证明候选已可靠落库，不表示正式计划已接受，也不表示已执行知识任务。

返回变量 accepted 是“通过校验、准备接受的 AcceptedPlan 对象”，还没有正式入账。

### 5.5 T4：正式接受与运行绑定

按顺序：

1. 重新鉴权并检查运行锁。
2. 从历史中按 attempt_id 找到 COMMITTED 尝试。
3. 重新读取候选、验证摘要和契约，与待接受内存对象比较。
4. ledger.accept_plan 保存正式计划。
5. PlanningAcceptanceRow 关联候选、正式计划和本次接受 actor。
6. _bind_execution_run 创建/绑定正式 Run，登记 RunClaim，递增 current_generation。
7. 同一事务提交。
8. 提交后才把新 generation 赋给内存 guard。

其中任意数据库写入失败，T4 全部回滚；此前 T3 保存的候选仍可保留供恢复复用。

### 5.6 最后读取

load_accepted_plan 另开 Session，重新检查权限、读取正式计划，返回：

```python
{
    "investigation_plan": plan,
    "task_execution_results": [],
    "observation": None,
    "round_complete": False,
    "investigation_status": "OPEN",
}
```

这是从唯一正式事实加载本轮初始状态，不代表每次图恢复都会清空全部历史；
恢复入口结合 checkpoint 决定继续哪个节点，不能脱离 runner 任意调用该函数复写 State。

## 6. 为什么运行锁与事务行锁同时存在

| 锁 | 持有位置 | 生命周期 | 作用 |
|---|---|---|---|
| session advisory lock | 专用 AUTOCOMMIT 连接 | 同一 runner 调用期间，跨 T1–T4 和外部调用 | 同一 run 的正常执行者互斥 |
| FOR UPDATE 行锁 | 业务 Session | 当前事务提交/回滚结束 | 避免已有 Attempt/Run 行被并发修改 |
| 唯一约束/FK/触发器 | PostgreSQL | 写入时强制检查 | 拦截重复、错归属、非法状态变化与历史覆盖 |

T2 commit 会释放该事务行锁，但不会释放另一个专用会话持有的运行锁。
不会为等待模型而保持业务长事务；运行锁本身仍占用专用连接。
正常退出会关闭/失效化专用连接；进程死亡后由 PG 确认会话结束再释放锁，网络故障时不保证立即释放。
advisory lock 是协作锁，不会自动禁止绕过协议的人工 SQL；数据库约束承担另一层保护。

## 7. 场景 A：用户连续点击两次开始

前提：同一案件、轮次解析为同一个 run_id。

| 时刻 | 请求 A | 请求 B | 结果 |
|---|---|---|---|
| 0 秒 | 获取运行锁成功 | 未开始 | A 可进入规划 |
| 1 秒 | 模型等待中 | 尝试同一锁 | B 获取失败 |
| 2 秒 | 继续等待 | 抛 RecoveryBlockedError | B 不调用模型、不建第二套规划 |

不能只断言页面出现一次结果。验证应检查 B 被拒绝、模型调用计数为 1、没有多余 Attempt。
如果 B 在 A 完全结束释放锁后才进入，则允许接续，并从已保存结果复用，不再生成计划。

对应测试：test_concurrent_initial_planning_is_blocked_by_same_round_lock。

## 8. 场景 B：STARTED 提交后，模型调用前或调用中崩溃

```text
T2 提交：Attempt A1 = STARTED，claim=C1
→ 进程退出，尚无 COMMITTED
→ 新进程取得运行锁，planning_claim_id=C2
→ T1 发现 A1.claim_id != C2
→ A1 结算 UNCERTAIN
→ T2 默认策略阻止重试
```

即使实际上尚未发出请求，本地也不能仅靠 STARTED 证明这一点，所以保守处理。
显式允许 retry_uncertain 后，新建 A2；A1 保留 UNCERTAIN，原 actor 不改成新 actor。

验证：默认无第二次模型调用；显式允许后多一个新 Attempt，旧记录不变。
对应测试：test_unknown_planning_interruption_requires_explicit_retry_and_preserves_actor。

## 9. 场景 C：候选已提交，正式接受前退出

| 步骤 | PostgreSQL 状态 | 恢复动作 |
|---|---|---|
| A 模型完成 | 尚未保存 | 不能仅凭内存复用 |
| T3 提交 | A1=COMMITTED，候选及摘要已保存 | 候选可复用 |
| A 进程退出 | 无正式 AcceptedPlan | B 重新拿锁 |
| B 进入 T1 | 找到 COMMITTED 候选 | 验证后直接返回，跳过模型 |
| B 进入 T4 | 正式计划、来源、Run 一起提交 | 保留原 task_id |

候选生成者和正式接受者可以不同，两者审计分别保存；B 必须重新拥有 READ/CONTINUE 权限。
验证：模型总计只调用一次，任务 ID 与原候选相同，acceptance 指向原 attempt。
对应测试：test_committed_candidate_or_acceptance_survives_missing_node_checkpoint；
test_actual_process_exit_after_candidate_commit_resumes_without_replanning（真实子进程退出）。

## 10. 场景 D：正式接受成功，但 checkpoint 还没保存就崩溃

```text
T4 commit：AcceptedPlan + Acceptance + Run/Claim 全部成功
→ 内存赋值或节点返回/checkpoint 前进程退出
→ B 从 PG 发现正式计划与运行
→ _claim 绑定新代次，例如 1→2
→ prepare_plan 不走 generation==0 分支
→ 加载原正式计划，不重新调用初始模型
```

检查：同一计划、同一 task_id、同一接受来源；新 run claim 不覆盖旧 claim。
对应测试：test_committed_candidate_or_acceptance_survives_missing_node_checkpoint 的 acceptance 分支。

## 11. 场景 E：正式接受前，旧持锁连接被终止

这里必须区别于正式 Run 的 1→2 例子。

| 时刻 | A | B | 数据库 |
|---|---|---|---|
| 0 秒 | guard.generation=0，claim=C1，等模型 | 未开始 | A1=STARTED，无正式 Run |
| 5 秒 | 持锁 PG 会话被终止，HTTP 仍可继续 | 未开始 | 运行锁释放后可被获取 |
| 6 秒 | 仍等模型 | 取得锁，guard.generation=0，claim=C2 | 旧 A1→UNCERTAIN |
| 7 秒 | 仍等模型 | 显式允许重试，新建 A2 | A1 不被覆盖 |
| 10 秒 | 仍等模型 | 保存候选并正式接受 | 第一份正式 Run 通常 generation=1 |
| 20 秒 | 模型返回，尝试 T3 | 已完成 | A 的 check_lock 失败，不允许保存 |

B 没有显式重试许可时会停在策略检查，不会自动重新调用模型。
检查：A 无法保存旧候选；B 的正式计划唯一；A1 保留 UNCERTAIN，A2 为 COMMITTED。
对应测试：test_lost_planning_lock_fences_old_candidate_after_new_actor_finishes。

## 12. 场景 F：正式运行已有 generation=1，旧执行者恢复活动

```text
A generation=1，等待外部模型
→ A 持锁连接终止（Python/HTTP 不一定停止）
→ B 取得锁并提交 _bind_execution_run：1+1=2
→ B 内存 generation=2
→ A 模型返回，尝试提交
→ check_lock 发现旧连接无效，拒绝
```

若继续执行代次检查，则 A 的 1 与数据库 2 不匹配也应拒绝；实际可能在锁检查就抛错，
不代表两次检查必然都执行。generation 校验必须由提交端执行才有效，不是数字本身有魔力。
这属于后续正式 phase 的防护，用来理解 prepare_plan 绑定代次的目的；
prepare_plan 接受前自身主要调用 check_lock，不能误写成它已执行 guard.check 的 Run 行比较。

检查：旧产物不能被保存成当前有效结果；不保证远端请求停止或不计费。
对应测试：test_lost_lock_connection_fences_old_worker_after_new_generation_finishes。

## 13. 场景 G：生成期间撤权与授权窗口

### 已撤权能拦住

```text
A 有 READ + CONTINUE，T2 保存 STARTED
→ 模型等待期间管理员撤销 CONTINUE
→ 模型返回
→ T3 _authorize 拒绝
→ 不保存成功候选，不接受计划，不执行知识任务
```

因为结算路径本身需要授权，可能保留 STARTED，而不是强行标记 FAILED；之后有权限的恢复者处理。
验证：无 AcceptedPlan、无知识执行 Attempt；不能把这个状态误报为计划已完成。
对应测试：test_revoked_continue_after_model_never_accepts_or_executes。

### 极窄窗口仍然存在

T3 授权通过后立刻撤权，T3 仍可能提交候选；T4 的重检可能随后拒绝正式接受。
T4 授权通过后到 T4 commit 前再次撤权，也仍存在窗口。不能声称这些重检保证原子撤权。

## 14. 场景 H：只有读取权限，试图恢复执行

```text
Actor B 可以 READ 历史
但 CONTINUE 返回 False
→ runner 的继续鉴权失败
→ 不能因为能看到历史，就允许创建新尝试
```

验证：受控只读历史入口可以按权限读取；执行 runner 必须分别通过两项权限，失败不进入模型/工具。
对应测试：test_resume_checks_both_permissions_before_reading_checkpoint；test_access_postgres.py 的读/继续分权测试。

## 15. 场景 I：伪造上下文或候选

| 输入问题 | 拦截位置 | 应验证的结果 |
|---|---|---|
| 调查 I1 配上 I2 的 Round | check_runtime / FK | 调用模型前拒绝 |
| 案件 model_id=M1，知识 scope=M2 | KnowledgeSearchTool.validate_binding | 调用前拒绝 |
| 上下文案件内容与 PG canonical 内容不同 | _bound_context 比较 | 拒绝访问 |
| 重复 task_id、空目标、不支持能力、非 PENDING | planning.validate / AcceptedPlan | 不接受、不执行任务 |
| 内存待接受对象与已保存候选不同 | T4 expected != accepted | 拒绝接受 |
| 不同正式计划内容被强行关联到候选 | 数据库 acceptance trigger | 写入失败，事务回滚 |

确定性检查不证明 statement/goal 的语义正确；越权人工 SQL 若同时改动数据与摘要，也不能只靠 hash 发现。
对应测试：test_runtime.py、test_tool_runtime.py、test_invalid_initial_plan_is_not_accepted_or_executed、
test_acceptance_cannot_bind_different_plan_content_to_committed_candidate。

## 16. 场景 J：接受事务写到一半失败

```text
accept_plan 已执行 flush
→ 保存 Acceptance 或绑定 Run 时失败
→ T4 rollback
→ T4 内新增正式计划/来源/运行变更一起回滚
→ T3 早已提交的候选仍保留
```

flush 不是 commit。没有“accept_plan 函数返回就一定永久接受”的保证，外层事务才是提交边界。
恢复应从候选复用再尝试接受，不能重新生成 task_id。
这条是事务原子性的推导；当前测试有接受来源错配回滚验证，但不能把它说成已覆盖
每一条 SQL 语句前后的全部故障排列。

## 17. 数据库约束与事务的最后防线

- PlanningRun：run_id 主键；round_id、plan_id 唯一；案件/轮次复合 FK；稳定规格不可原地修改。
- PlanningAttempt：同 run 的 attempt_number 唯一；部分唯一索引限制至多一个 STARTED 和一个 COMMITTED。
- Attempt 的状态与结束时间、候选正文/摘要存在性保持一致。
- STARTED 只能结算到合法终态，终态与历史不能原地覆盖。
- Acceptance 关联同一 run/plan/investigation/round 和 COMMITTED attempt。
- 迁移 0007 的 trigger 校验候选与正式计划内容匹配、actor 归因和摘要形状等。

约束不是跨 PG 与外部模型的分布式事务，不会自动撤销远端模型调用。

## 18. 不保证什么，以及可优化什么

1. 不保证外部 API exactly-once。未来有副作用的工具必须让接收端支持稳定幂等键。
2. 不保证 timeout 后同步线程与远端调用停止。
3. 不保证会话未结束的挂死 worker 可以被自动接管；lease/hung-worker takeover 尚未实现。
4. 不保证外部权限撤销与本地提交严格原子一致。
5. 不把 checksum 当作防数据库管理员篡改的数字签名。
6. 当前异常/重试策略为 run-wide，不是每工具/每操作细粒度策略。
7. 授权链重复加载与同 action 重复校验可收敛，但不能删掉必要的 READ/CONTINUE 与提交前重检。
8. prepare_plan 保持首轮知识能力边界，不等于多轮调查和记忆系统已实现。

## 19. 如何复核测试证据

测试位置：

- [初始规划 PostgreSQL 故障测试](../backend/tests/workflow/test_initial_planning_postgres.py)
- [正式执行与恢复 PostgreSQL 测试](../backend/tests/workflow/test_recovery_postgres.py)
- [资源授权与账本访问测试](../backend/tests/workflow/test_access_postgres.py)
- [通用身份测试](../backend/tests/workflow/test_runtime.py)
- [工具绑定测试](../backend/tests/workflow/test_tool_runtime.py)

前一轮解释时实际运行过的针对性测试：10 passed、33 deselected，1 条既有依赖警告。
该组包含并发重复执行、锁连接终止、阶段提交后 checkpoint 前崩溃、UNCERTAIN 策略、
规划期间撤权、候选/接受后缺失 checkpoint，以及候选提交后的真实子进程退出。
它不是本文全部场景每一个细分排列的覆盖证明。本次仅写文档，未重新运行这些测试。

建议手工/自动故障注入统一检查：

```text
允许哪些数据库事实留下？
哪次外部调用允许发生？
是否允许新增 Attempt？
旧 actor、task_id、候选、正式计划是否保持原值？
失败发生在 commit 前还是后？
恢复后是否读取原事实，还是错误地重新生成？
```

不要只断言“抛了异常”或“最后有一份计划”，应同时断言副作用次数和数据库终态。

## 20. 记忆卡片

```text
授权：这个人能不能做。
运行锁：同一 run 当前是否已经有人在做。
行锁：这一小段事务内，已有数据行能否被并发修改。
claim_id：正式接受前，区分是哪次规划执行权留下的尝试。
generation：正式 Run 的执行代次，拒绝过期执行者。
STARTED：准备执行已经可靠记账，不证明外部请求一定发出。
COMMITTED 候选：候选可靠保存，不等于正式计划已经接受。
AcceptedPlan：已经正式接受的计划事实，不等于任务已执行。
checkpoint：图推进到哪里，不替代执行账本。
```

相关说明：[图内初始规划与恢复](workflow-initial-planning-recovery.md)、
[可信环境与知识工具](workflow-trusted-tools.md)。
