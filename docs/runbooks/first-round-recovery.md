# Stage 8C：首轮崩溃恢复

状态：已实现，待 review。现已扩展到图内初始规划；案件与轮次须先入账，案件保持 OPEN。
没有多轮 Replanning、自动重试、Celery 或恢复 Web API。已有正式计划的执行与恢复继续兼容。

## 1. 代码组成

| 文件 | 职责 |
|---|---|
| `workflow/graph.py` | 唯一持久化拓扑，prepare_plan 在图内准备或读取计划 |
| `workflow/state.py` | 共享业务状态与检查点身份 |
| `workflow/invocation.py` | 每次调用的可信 Runtime 包装，恢复控制信息不进入 State |
| `workflow/recovery.py` | 受控恢复入口、ledger phase executor、逐阶段入账、运行独占和代次 fencing；不建图或重复任务循环 |
| `workflow/checkpoints.py` | 官方 AsyncPostgresSaver 的显式创建/初始化；JSON-only 序列化 |
| `workflow/persistence/recovery_contracts.py` | FirstRoundSpec、RecoveryPolicy、RunClaim 与 PhaseArtifact v2 |
| `workflow/persistence/recovery_models.py` | 运行身份、调用代次、Attempt 归属与不可变结果选择 |
| `workflow/persistence/repository.py` | 显式读取 v1/v2 产物；保存 Query 失败事实 |
| `workflow/nodes.py` | 唯一任务循环与 Observation 业务入口；原查询形成/错误分类与 7A 路径复用 |
| `alembic/versions/20261103_0006_first_round_recovery.py` | 增量迁移、FK/唯一性、不可变与代次约束 |
| `workflow/persistence/planning_*` | 计划前稳定输入、尝试与候选、正式接受的来源绑定 |
| `alembic/versions/20261104_0007_planning_journal.py` | 规划日志表、不可变/完整性与候选接受约束 |

FirstRoundRecovery 是安全入口，不另建调度器。节点与推进仍全部由 LangGraph 执行：

```text
prepare_plan
    已有正式计划 → 加载并核验
    没有正式计划 → Agent 候选 → 确定性校验 → 候选入账 → accept_plan
    ↓
task_execution
    每个 Task：Query phase → Execution phase
    ↓
Observation phase
    ↓
replanning（当前仅结束本轮，案件 OPEN）→ END
```

8D-1 统一为 graph.py 中同一份拓扑定义；FirstRoundRecovery 通过同一个 builder 编译图，
不是强制所有调用共享同一个 compiled object。8D-2 进一步统一任务循环和 Observation，
随后已删除 direct 生产路径；首次持久化执行和恢复均经过同一 ledger executor。
正式执行入口现在只有 FirstRoundRecovery.ainvoke；原非持久化入口已移除。
build_investigation_graph 仅供 runner 内部构图，必须配置账本与 checkpoint，并提供入口生成的有效 claim。
prepare_plan 现在在图内生成候选并正式接受，也能直接读取已有计划；单元测试显式注入阶段替身。
调查和轮次仍由可信调用方提前登记；初始计划不再要求在图外手工准备。详细恢复边界见[图内初始规划](../workflow-initial-planning-recovery.md)。

## 2. 调用方式

先安装 backend 的 `database,knowledge,recovery` 可选依赖，并通过 Alembic 升级业务账本。
checkpointer 自有表由官方 saver.setup 显式初始化，与 Alembic 业务表是不同的迁移体系。
不要每个业务请求都 setup；建议在受控部署步骤对指定数据库/schema 执行一次。

```python
from risk_platform.workflow.checkpoints import postgres_checkpointer
from risk_platform.workflow.recovery import FirstRoundRecovery
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy

# Investigation、Round 已通过原仓储可靠提交；新调查不要求事先有 AcceptedPlan。
# context 通过 8B load_runtime_context 从 canonical 记录加载，当前 actor 已认证。
async with postgres_checkpointer(checkpoint_database_url, setup=False) as saver:
    runner = FirstRoundRecovery(
        investigation_agent,
        session_factory=sessions,
        checkpointer=saver,
    )
    invocation = InvestigationInvocation(runtime=context)
    result = await runner.ainvoke(context=invocation)
    accepted_plan_id = result["plan_id"]
```

runner 和 graph 可以复用，当前 actor/权限/预算按每次调用提供。
Agent 必须固定 model；规范 Agent 的 query/observation prompt 文本摘要也写入 FirstRoundSpec。
该 runner 在没有正式计划时于 prepare_plan 内调用 initial_plan；已有正式计划或已提交候选不会重新生成。
测试使用替身，不证明自然语言语义质量。

新规划 thread_id 从 investigation/round 生成，plan_id 内部预留；旧已接受计划沿用原 investigation/round/plan 定位。
不接受外部自由传入的 thread_id、checkpoint_id、
任意 State、Command 或跳步更新。不要把内部 `_graph.aget_state/aupdate_state` 暴露为公共 API。
对外结果仍为类型化的 planning input、plan、execution results、observation 和轮次状态。

## 3. 恢复前与恢复中分别鉴权

```text
当前 CONTINUE
AND 当前 READ
AND canonical 调查/轮次/计划归属
AND model_id/scope/profile/数量限制
    ↓
才读取 checkpoint 或复用历史 payload
```

仅 READ 不能恢复推进；仅 CONTINUE 也不能借恢复读取历史。
读取 checkpoint 之前、每个 phase 进入及提交之前、最终结果返回前都会重新校验。
运行中撤权不能撤销已发出的 API 请求，但会阻止继续提交/向调用方返回未获授权的产物。
知识执行仍调用封板的 7A，并保留独立 knowledge:search 权限检查。

本版 FirstRoundSpec 固定计划、业务 scope/profile、Query max_top_k 与模型/prompt。
actor/timeout 变化不改变业务摘要；业务范围或 Query max_top_k 等改变则明确冲突，不能静默套用旧输入。
当前权限不足时不返回部分结果，也不重新搜索来绕过历史 READ 限制。

## 4. 四张控制/审计表

| 表 | 保存内容 |
|---|---|
| investigation_runs | 一轮一个固定 plan 的运行定义和当前代次 |
| investigation_run_claims | 每次进入的代次、actor 与显式恢复策略，不可变 |
| investigation_run_attempts | 新 Attempt 关联到哪个运行/代次，不可变 |
| investigation_run_selections | 本轮每个 Operation 固定使用哪个 Result，不可变 |

这些表只约束执行事实和并发接续，不创建新的任务调度状态机。
Run 的业务载荷不可改写；唯一可更新控制字段为严格递增的 current_generation。
复合 FK 将运行与原调查/轮次/计划绑定，Attempt/Result 选择也必须属于同一计划。
旧 0004/0005 和 v1 Operation/Result 不改写；新迁移扩展 Result 契约支持 v2。

## 5. phase 的事务边界

```text
事务 1：鉴权、固定操作输入、检查/选择已有产物、结算可证明失联的旧尝试
    commit

事务 2：检查显式策略、登记 STARTED + 本次 actor + 运行代次归属
    commit

调用 Query / 7A / Observation

事务 3：重新鉴权与 fencing
        Result + Attempt.COMMITTED + 本轮结果选择
    commit

最后节点返回 JSON State 更新 → checkpoint
```

不会在模型网络调用期间持有长数据库事务或行锁。
业务产物已提交而 checkpoint 没推进时，节点重入直接读取并复用原事实。
Query、Execution、Observation 都有独立 Operation/Attempt/Result，T1 崩溃不会使 T0 重复调用。
ledger 和 checkpoint 不承诺跨连接原子提交。

## 6. 复用与显式选择

一份已提交产物可以在当前授权下固定使用。
多份 COMMITTED 不会自动取最新或最新成功；首次选择必须明确指定 attempt_id：

```python
policy = RecoveryPolicy(selected_attempts={operation_key: chosen_attempt_id})
result = await runner.ainvoke(plan_id=plan_id,
                             context=InvestigationInvocation(runtime=context), policy=policy)
```

选择保存到 investigation_run_selections 并关联本次 claim。
后续不能因有了更新结果或更换 actor 就替换本轮已固定的选择；选择不一致时冲突。
repository 仍返回全部历史，不承担“哪个历史结果是最终答案”的策略。

COMMITTED 不等于成功：FAILED、REJECTED、零命中和 INCONCLUSIVE 都作为已提交事实复用，
不会因为依赖后来恢复或权限后来放开而自动重跑。
如果需要重新调查，应另行接受明确的新业务工作，而不是覆盖已恢复轮次。

## 7. STARTED / UNCERTAIN 与显式重试

默认 RecoveryPolicy 不允许重试不确定或无产物的失败尝试。

只有确认 STARTED 属于同一 Run 的旧代次、且本次已获得该 Run 独占权时，才结算为：

```text
UNCERTAIN + PREVIOUS_RUN_INTERRUPTED
```

这次结算先提交，即使随后因默认策略阻止重试，也保留真实不确定历史。
没有 Run 归属的 legacy STARTED、其他 Run 或当前代次仍活跃的尝试会阻止执行，不能自行推断为失联。
这些情况需要可信人工/业务服务明确处理；本版不自动接管所有 8A 历史尝试。

明确允许知识读取/模型调用再次尝试时：

```python
policy = RecoveryPolicy(retry_uncertain=True)
```

创建新的 Attempt，由当前 actor 归因；旧 Attempt 不改成成功。
未知尝试可能已完成外部调用/计费，重试可能再次收费。不能宣称 exactly-once 外部调用。
未提交的知识执行重新尝试会重新 resolve 当前 route，必须保留新 Attempt 的新快照，不能伪装成旧请求恢复。
已提交 T0 的旧 snapshot 始终保持不变。

## 8. Query 已知失败也必须可重放

v1 CommittedArtifact 保持原样。新增 PhaseArtifact v2，允许 Query Operation 保存：

```text
query_request
或者
TaskExecutionResult(status=FAILED, error.code=QUERY_FORMULATION_*)
```

这只是查询形成失败事实，不代表已经调用知识工具。
失败 execution_id 同样稳定入账，Observation 可引用其失败摘要；恢复不重新生成 UUID 或再次调用 Query 模型。
数据库与 Python 均限制 Query 失败产物的 task、capability、状态和错误代码范围。

## 9. 并发与过期 worker

每个 Run 使用 PostgreSQL 会话级 try-advisory lock；相同轮次并发恢复明确拒绝。
锁使用独立 NullPool 连接，不占用 ledger 短事务连接池，避免多个长请求耗尽自己的业务池。
退出时关闭锁连接；进程死亡后 PG 释放连接锁。这里没有定时租约或后台抢占器。

每个 phase 提交前同时验证锁连接/PID/锁状态，并锁住 Run 行检查 current_generation。
锁连接丢失后，旧 worker 即使外部请求返回，也不能跨新代次提交。
代次 fencing 不能撤销已经发生的外部调用，也不能阻止一切重复费用。

## 10. checkpoint 的安全与完整性

恢复 checkpoint 只保存 JSON wire state，8D-1 使用 checkpoint_schema_version=2 标识统一拓扑。
旧 8C v1 检查点不会被自动转换；明确拒绝并要求显式迁移，不能删除历史或改 thread_id 绕过。
本次不提供旧检查点迁移工具；部署前必须确认在途运行和旧检查点，不能宣称滚动兼容。
独立的 load_plan / finish_round 待执行节点同样明确拒绝，不静默跳过。
不保存 actor、authorize 函数、客户端、数据库连接或任意 Python 类构造信息。
JSON serializer 拒绝 pickle/msgpack/未知类型和非有限数值；任务错误只保存异常类型，不持久化原始异常正文。

读取后，将 checkpoint 的计划、任务状态、执行结果和解释逐项对照已固定的 ledger Result。
checkpoint 宣称完成而账本缺结果/选择，或 execution_id/payload/Observation 不一致，均完整性失败。
checkpoint 消失但 ledger 完整时可以从 Accepted Plan 重入，复用已确认事实，而不是重新规划。
完成后再次调用只返回通过当前权限校验的同一结果，不重复外部调用。

## 11. 部署与验收边界

0006 和 checkpointer.setup 都只在测试 UUID schema 验证，不自动迁移业务库。
0006 在存在运行历史或 v2 产物时禁止 downgrade 丢弃事实。
P2-10 继续保留：旧二进制写入/读取新契约并不是自动支持的 rolling deploy；生产前需协调切换或另行 expand/contract。

真实 PG 测试覆盖：重建 saver/engine/graph 后恢复、三个 phase 提交后 checkpoint 前崩溃、
真实子进程 os._exit、T0 复用/T1 显式重试、旧/新快照、失败/拒绝/零命中、Query 失败稳定身份、
READ/CONTINUE 先于 checkpoint、checkpoint 损坏、多结果显式选择、并发接续、旧 worker fencing 及单连接业务池。
模型与检索使用替身，未调用付费 LLM/Embedding，不作为检索/解释质量评测。

2026-11-03 最终实测：backend 全量 497 passed、7 skipped；新增恢复用例 20 项均在其中通过。
其中包含真实 PostgreSQL/官方 PostgresSaver 和真实子进程退出测试，不只使用内存 saver。
只有一条既有 LangChainPendingDeprecationWarning；git diff --check 和新增文件空白检查通过。
测试使用独立 UUID schema，未自动升级业务库；未进行生产规模压测。

运行：

```powershell
$env:RUN_POSTGRES_INTEGRATION = "1"
# RISK_DATABASE_URL 指向专用测试 PostgreSQL。
python -m pytest backend/tests/workflow/test_recovery_contracts.py backend/tests/workflow/test_recovery_postgres.py -q
python -m pytest backend/tests -q
git diff --check
```

P2-11 的权限组合已实现为 CONTINUE + READ，待本阶段 review；P2-8 本版选择 ledger phase 重入，
不引入 LangGraph Functional task。生产规模压测、更多故障注入与生产部署仍需后续验收。
