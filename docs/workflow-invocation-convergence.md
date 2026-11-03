# Stage 8D-3：Fresh / Resume 调用入口语义收敛

状态：已实现，待 review。P2-15 的声明/实际上下文类型差异已在实现中消除，关闭状态待 review 确认。

后续变更：下文第 2 节是 8D-3 阶段的历史接口，现已退役。正式执行只使用第 3 节的持久化 runner；
当前边界见 [移除非持久化入口](workflow-persistent-only-boundary.md)。
计划生成现已纳入图内，最新调用与接受前恢复规则见 [图内初始规划](workflow-initial-planning-recovery.md)。

## 1. 唯一上下文契约

图声明 `context_schema=InvestigationInvocation`，各节点使用 `Runtime[InvestigationInvocation]`。
图与持久化入口均不再自动包装裸 `InvestigationRuntimeContext`。

```python
invocation = InvestigationInvocation(runtime=trusted_runtime)
```

`trusted_runtime` 仍由可信应用认证 actor、加载 canonical 记录并提供真实授权策略后建立。
包装对象不是认证凭据，不能从模型或 HTTP body 的身份声明直接生成可信授权。

`require_invocation()` 核验包装及其中 runtime 的实际类型。持久化公开入口拒绝所有预填 recovery claim；
只有鉴权、加锁之后，该入口才生成内部 claim 并传给 compiled graph。图还检查 claim 类型和 runtime 身份一致性。

LangGraph 自身可能将符合 context schema 的 mapping 构造成 dataclass；节点拿到后仍只接受统一包装类型，
且其中 runtime 必须是真实 InvestigationRuntimeContext。JSON 中伪造 actor 的字典不能替代可信 runtime。
这不是继续兼容裸 Runtime 的另一条调用分支。

## 2. 非持久化调用

```python
graph = build_investigation_graph(agent)
result = await graph.ainvoke(
    {"planning_input": trusted_runtime.investigation.planning_input},
    context=InvestigationInvocation(runtime=trusted_runtime),
)
```

保留原普通流程：Initial Planning、共享任务节点、Observation、replanning。
它仍然使用 direct_phase，不自动创建 ledger 身份、接受计划或提供去重保证。

这是非持久化业务入口的保留，不是上下文双类型兼容；两种执行保障不能仅因包装相同就被混淆。

## 3. 持久化首次执行与恢复使用同一次调用契约

```python
runner = FirstRoundRecovery(agent, session_factory=sessions, checkpointer=saver)
invocation = InvestigationInvocation(runtime=trusted_runtime)

# 第一次调用和进程重启后的恢复调用，接口相同。
result = await runner.ainvoke(plan_id=accepted_plan_id, context=invocation)
```

保留 FirstRoundRecovery 类名，不为改名再增加一套接口或 Factory。
它既能执行没有 checkpoint 的正式计划，也能恢复中断运行，不要求调用方判断恢复位置。

| 数据库历史 | 同一个入口的行为 |
|---|---|
| 没有 checkpoint / 阶段历史 | 从 Accepted Plan 加载，逐阶段登记 STARTED 并执行 |
| 有中断 checkpoint | 校验后继续图；阶段账本决定复用、阻断或执行 |
| 图已结束且事实完整 | 验证 checkpoint 和账本，直接返回原产物 |
| 有 UNCERTAIN 或多份待选择结果 | 沿用明确恢复策略，不自动挑选最新成功 |

调用方不提供 `mode=FRESH/RESUME`、thread_id、恢复节点或任意 State。
run_id 仍由 investigation/round/plan 的稳定身份确定。Accepted Plan 前置条件、READ 与 CONTINUE 分权、
actor/scope/model 绑定和每次 claim 的 actor attribution 均保持。

可选 `policy` 仍是可信调用参数，保留原来的显式 retry / selection 规则，不写入模型 State。
公开 invocation 的 recovery 始终为 None，入口生成的 claim 不回填、改写调用方对象。

## 4. 从旧调用迁移

```python
# 旧代码：不再接受
context=trusted_runtime

# 新代码：明确包装
context=InvestigationInvocation(runtime=trusted_runtime)
```

这是一项显式 Python 调用契约变更。应用、脚本和测试需要同步更新，不提供静默 fallback。
访问层 `load_runtime_context`、read/register/start 等内部权限 API 仍使用原有 Runtime 类型；它们不是图调用入口，
不应为了形式统一而额外包一层。

不修改数据库 schema、已有 operation digest、Attempt/Result、checkpoint v2 或节点 topology。
旧 v1 检查点拒绝策略保持不变，也不解决跨版本 rolling deployment。

## 5. 验证重点

- 裸 Runtime、缺失上下文、伪造 runtime 字典和伪造 claim 在规划前拒绝。
- 持久化公开入口在数据库和 checkpoint 访问前拒绝非法包装或预填 claim。
- 同一 public invocation 首次执行中断后再次调用，不设置恢复模式；内部 fresh 传入 bootstrap，resume 传 None。
- 公共包装保持 immutable，内部 claim 不泄露到公共上下文或 checkpoint。
- 保留多 actor 并发隔离、权限撤销、缺少计划、结果复用、真实 PostgreSQL 与 os._exit 故障回归。

P2-10（rolling）、P2-12（run-wide retry）、P2-13（hung-worker takeover）、P2-16（旧拓扑真实兼容测试）继续保留。
不改节点业务逻辑或三个事务，不进入 Stage 9。

实际验证：backend 全量启用真实 PostgreSQL，520 passed、7 skipped，1 条已有依赖弃用警告。
包含 26 个恢复集成用例、真实 os._exit，以及 10 个新增上下文契约用例。git diff --check 通过。
