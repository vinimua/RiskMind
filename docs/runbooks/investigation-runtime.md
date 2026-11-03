# Stage 8B：每次运行的可信上下文与账本访问

状态：Stage 8B SEALED，review P1=0。未接入 checkpointer、节点重放、自动重试或恢复 API。
8A 的 0004 迁移与原 v1 契约不改写；新增 0005 做向前兼容演进。

## 1. 代码组成

| 文件 | 作用 |
|---|---|
| `workflow/runtime.py` | 每次运行的可信资源、身份、范围与读/继续权限检查 |
| `workflow/graph.py` | 共享 compiled graph 从本次 Runtime 取得 context |
| `workflow/nodes.py` | 每个任务形成 Query 前执行当前案件权限检查 |
| `workflow/access.py` | 加载 canonical 调查上下文，受控读取历史/登记操作/创建 Attempt |
| `workflow/persistence/business_inputs.py` | v2 稳定业务输入、v1 显式投影、Attempt 审计契约 |
| `workflow/persistence/repository.py` | 按版本读取操作，保留旧摘要，保存本次 Attempt actor |
| `workflow/persistence/models.py` | Operation 创作者与 Attempt 运行审计列 |
| `alembic/versions/20261103_0005_runtime_identity.py` | 增量迁移、审计校验与 downgrade 防护 |

本模块不实现登录认证。可信应用必须先确定 authenticated actor，再构造 context 和真实权限策略。
模型、HTTP 请求体、State 或 thread_id 不能自行成为可信身份来源。

## 2. 图调用方式

```python
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.recovery import FirstRoundRecovery

# Investigation、Round 已入账；计划由图内 prepare_plan 生成并正式接受。
runner = FirstRoundRecovery(investigation_agent, session_factory=sessions, checkpointer=saver)

async with sessions() as session:
    runtime_context = await load_runtime_context(
        session,
        investigation_id=investigation_id,
        round_id=round_id,
        execution=current_execution_context,
        authorize_resource=resource_policy,
        authorize_knowledge=knowledge_policy,
        action=InvestigationAction.CONTINUE,
    )

result = await runner.ainvoke(
    context=InvestigationInvocation(runtime=runtime_context),
)
```

execution 的 actor/scope/profile/预算由可信调用方提供，不从 Agent 返回值构造。
8D-3 起图调用必须显式包装 InvestigationInvocation，不再兼容裸 Runtime 自动包装。
非持久化图入口已退役；首次持久化执行和恢复统一使用上述 runner。当前权限必须同时允许 READ 和 CONTINUE。
如需明确接续既有正式计划，可额外传 plan_id；新调查无需提前准备计划。
authorize_resource 接收 `(actor_id, action, InvestigationResource)`；authorize_knowledge 继续使用 7A 的签名。
两者都必须显式返回 True，不能用 1、非空对象或异常代替授权。

graph 不再接收构图时的 execution_context/authorize 参数，也没有兼容性默认身份。
规划、任务执行、解释开始时会检查本次 context；每个任务生成 Query 前还会重查继续权限。
知识调用仍进入原 7A，并保留独立 knowledge:search 检查。
未改变 objective → Query Formulation → ToolRequest → 7A 的顺序。

State 不增加认证凭据或策略函数，仍只有规划/执行/解释及轮次状态；案件保持 OPEN。
当前图调用仍是首轮执行，不会根据账本自动跳过旧任务，不是恢复入口。
身份按次提供不意味着所有依赖天然线程安全；共享 Agent/client 必须遵守其自身并发约束，不保存当前 actor。

## 3. 案件目标与 scope

load_runtime_context 从 PG 读取并验证不可变 Investigation / Round 事实，检查二者归属。
每次受控账本访问还会重新加载并核对 canonical 事实，不能换一个 planning_input 就改变案件目标。

```text
canonical MonitoringAlert.model_id 或 UserQuery.model_id
    == execution.scope.model_id
    == 本次规划输入对应目标
```

案件有 model_id 时，scope.model_id 缺失和不一致都拒绝。
案件没有 model_id 时，可信应用必须明确传 allow_unscoped=True，并仍通过资源策略授权；不是默认全库放行。
不增加 tenant/time window 新字段，不能宣称已经支持这些隔离维度。

Context 可由可信应用直接构造用于内部测试；生产入口应从 canonical 仓储加载，不能相信请求体自报的 InvestigationRecord。
缺少 context、只读权限、输入错案件/模型/轮次时，图在模型调用前拒绝。

## 4. 读历史和继续执行分开

| 操作 | 权限 | 返回 |
|---|---|---|
| read_operation_history | investigation:read | 按原顺序的完整尝试历史与可读产物 |
| register_authorized_operation | investigation:continue | operation_key，不返回旧载荷 |
| start_authorized_attempt | investigation:continue | AttemptReceipt，仅身份与状态，不返回历史正文 |

读权限不授予新建 Attempt 权限，继续权限也不隐含读取完整历史。
这些入口仍是可信应用内部函数，不是 Web/resume API。
直接调用 8A repository 属于内部业务存储操作，不构成授权；应用对外入口必须经过受控访问层。

历史知识执行需满足当前 profile、数量与 scope；还会核对实际 Child 的 model/document/version/type。
Observation 的当前可读性取决于其全部支撑执行结果，不能因为解释已经保存就跳过检查。
当前 scope 变窄而不能覆盖原产物时整次拒绝，不裁剪部分 Context 后假装复用了原结果。

资源授权与范围检查在访问阶段/调用边界重新执行，不缓存为永久许可。
不承诺权限撤销能强行中止已经发出的上游请求；同步 SDK 超时和外部计费边界仍与原阶段一致。

## 5. v2 输入摘要与运行信息分离

v2 的 BusinessOperationInput 不包含 actor_id/timeout，也不包含 authorize 函数。

| 阶段 | 参与稳定摘要的阶段字段 |
|---|---|
| QUERY_FORMULATION | 模型、prompt_version、进入模型的 max_top_k |
| KNOWLEDGE_EXECUTION | candidate_request、稳定业务 scope、index_profile |
| OBSERVATION | 模型、prompt_version、按顺序关联的不可变 execution_refs |

共同部分仍包含 investigation/round/plan/task/operation 类型、固定 tool_name 和 schema_version。
不重复保存整个计划，FK 仍指向原不可变 Accepted Plan 与规范化任务。

同一业务输入在 A/B 之间接续，不因换 actor 或 timeout 改变摘要。
Query 的 max_top_k 实际进入模型，因此其变化仍冲突；Knowledge 的 scope/profile/请求变化也仍冲突。
当前运行 max_top_k 仍是执行/读取准入限制，不会因为从执行摘要移出就失去限制。

调用前可由候选请求和业务 scope 确定过滤范围，实际 effective request 仍以原 7A payload.request 为准。
不提前假造 route/collection，不改变每次检索固定路由快照的语义。

## 6. 创作者与每次尝试的实际发起人

v2 Operation.created_by 记录首次定义工作的人；之后 B 登记同输入仍复用该 Operation，不改写创作者。
新 Attempt 必须保存：

```text
actor_id
runtime_payload：调查/轮次、继续动作、本次完整执行 context（含预算）
runtime_digest
```

AttemptAttribution 是冻结且拒绝额外字段的类型契约，actor 必须与 execution_context.actor_id 一致。
runtime payload 使用 canonical JSON + SHA-256；不含凭据、客户端和策略函数。
数据库 INSERT trigger 校验归属、actor、动作和摘要；新 Attempt 缺 actor 会被拒绝。
原有终态触发器也保护这些新增字段，不能先启动尝试再换发起人或预算。
同 attempt_id 重放必须保持原 actor 与完整运行审计载荷，否则冲突；B 的显式重试必须使用新的 attempt_id。

start_authorized_attempt 与原 8A 一样仅 flush，调用方必须 commit 后才发起外部调用。
本阶段没有替工作流登记 Attempt，只有可供后续集成的受控仓储入口。
Result/Attempt.COMMITTED 仍由原账本同事务保存，COMMITTED 不等于业务成功。

## 7. v1 兼容

先按 v1 原始契约校验完整 payload 和 digest，再投影成明确的 v2 业务输入做比较。
投影不会写回旧 payload，不重算旧 digest，不把旧 schema_version 改成 2。
同逻辑 key 已有 v1 时，登记入口可以返回原记录，不新建相同身份的另一份工作。

旧 Operation 的 context.actor_id 仍保留原语义，不能拿当前 B 替换后交给 v1 API。
旧 Attempt 没有独立归因时，读取返回 actor_id=None、attribution=None，明确未知，不猜测是 Operation actor。
0005 不 backfill 旧 Attempt，不 UPDATE 旧 Result 或快照。
新尝试可以在当前 CONTINUE 授权和业务范围校验后关联旧 Operation，并独立记录 B。

发现输入不匹配、未知版本、摘要损坏、缺失关联或当前权限不足时拒绝，不退回默认 alias 或默认 actor。
完整历史仍由仓储返回；不会自动选择最新成功 Attempt 作为继续调查的答案。

## 8. 迁移与验证

0005 只增量扩展 Operation/Attempt 审计列与约束，不修改 Knowledge 或 0004 文件。
已有 v1 记录保持原文；新 v2 Operation 要求创作者，新 Attempt 要求发起人审计。
存在 v2/创作者/尝试审计数据时禁止 downgrade 丢弃这些事实；只有未产生新审计数据时允许退回 0004。

```powershell
$env:RUN_POSTGRES_INTEGRATION = "1"
# RISK_DATABASE_URL 指向允许创建临时 schema 的测试 PostgreSQL。
python -m pytest backend/tests/workflow -q
python -m pytest backend/tests -q
git diff --check
```

测试包含同图不同 actor/案件顺序及并发隔离、只读/继续分权、撤权、缺 context、model/scope 错配、
v2 摘要、真实 PG v1 数据升级、B 接续归因、新 Attempt 缺归因拒绝、审计不可变、旧 Result/快照保留与 downgrade 防护。
模型使用替身，不宣称自然语言语义质量已验证。

2026-11-03 实测：workflow 测试 147 passed；最终 backend 全量 477 passed、7 skipped。
PostgreSQL 集成在独立 UUID schema 中实际执行；跳过项为其他未启用的集成场景。
有一条既有 LangChainPendingDeprecationWarning；git diff --check 与新增文件空白检查通过。

## 9. 当前边界

8B 不实现自动恢复、checkpointer、自动重试、租约接管、多轮 Replanning、Celery 或 Web API。
只构建可信上下文/权限与审计边界，后续 8C 才把它们接到真正的恢复执行路径。
P2-4/5/9 已 review RESOLVED；model/scope 绑定仅覆盖当前过滤模型，不代表支持 tenant/time-window。
其他 P2 保留。下一步只进入首轮崩溃恢复设计与实现，不顺带做多轮 Replanning。

## 10. Review 保留项

P2-10（OPEN）：0005 兼容旧数据，不兼容旧 8A 应用的新 Attempt 写入。
数据库升级后，旧 replica 不带 attribution 的 INSERT 会失败。这不是 rolling-deploy 安全迁移。
将来部署必须协调暂停旧写入/排空旧实例、升级数据库和新应用后再恢复写入，
或者另行设计 expand/contract；本轮不放宽审计 trigger，也不改写 0005。
存在新审计数据后不能把直接 downgrade 当作应用回滚方案。

P2-11（OPEN，8C 前置）：把历史 Result 重新放回 Workflow State 所需的权限组合尚未实现。
8C 的建议规则为 CONTINUE + READ，并执行当前 scope/profile 校验；只有 CONTINUE 时不能静默读取历史 payload。
8C 已单独实现 CONTINUE + READ 的首轮恢复入口，待 review；8B 原始入口本身仍不是恢复接口。
具体支持范围见[首轮崩溃恢复](first-round-recovery.md)。
