# Stage 8D-2：Phase Execution Convergence

状态：已实现，待 review。只收敛阶段执行代码；不改变 topology、数据库 schema 或 checkpoint v2，不实现 Stage 9。

后续：8D-2 已 review SEALED。8D-3 已删除下文历史描述中的裸 Runtime 自动包装兼容，
当前调用示例见 [统一调用契约](workflow-invocation-convergence.md)。

## 1. 解决的问题

8D-1 已统一拓扑，但普通任务循环和恢复任务循环仍分别维护 Query → Search → TaskStatus。它们容易在输入校验、结果身份或失败处理上发生偏差。

本次删除 `recovery.recoverable_task_execution()` 与 `recoverable_observation()`，普通与持久化入口统一调用 `nodes.task_execution()`、`nodes.observation()`。

```text
唯一 Graph
    ↓
唯一 task_execution 业务循环
    Query → Knowledge Execution
    ↓
唯一 observation 业务入口
    ↓
同一 replanning
```

## 2. 业务步骤与执行机制分开

节点通过一个小型异步函数接口执行阶段：

```python
await execute_phase(kind, produce, task_id=..., candidate=..., results=...)
```

`kind` 是已有固定 OperationType；`produce` 是当前阶段的实际工作；其余字段用于稳定操作输入。没有 Registry、Factory 或新的流程调度器。

| 阶段 | produce 返回值 | 节点随后做什么 |
|---|---|---|
| QUERY_FORMULATION | QueryOutcome(candidate 或 failure) | 成功则检索，失败则直接记录任务失败 |
| KNOWLEDGE_EXECUTION | TaskExecutionResult | 校验、收集结果、映射 TaskStatus |
| OBSERVATION | RoundObservation | 写回状态，不确认案件根因 |

`QueryOutcome` 是节点内部的数据容器，不是新的数据库或对外模型契约。未经校验的原始 candidate 不会被直接入账为正式请求。

## 3. 两个机制，不再有两份任务循环

### 原普通兼容入口

`nodes.direct_phase()` 只调用 `await produce()`，保持原 7B/8B 无数据库执行路径。

这是兼容模式，不提供 ledger 去重、自动接受计划或恢复保证。

### 持久化首次执行与恢复

`recovery.ledger_phase_executor()` 将上述业务返回值适配为已有 PhaseArtifact，构建 BusinessOperationInput，再调用原 `_phase()`。

```text
节点业务函数
    ↓ produce 回调
ledger_phase_executor
    ↓ 产物适配
_phase：inspect → reuse / block / execute → commit
```

首次执行和恢复使用同一个 executor，不判断“是不是第一次”来选择另一套业务逻辑。

- 没有历史：创建 STARTED，调用共享业务函数，提交结果。
- 已有正式结果：复用，不调用共享业务函数中的外部服务。
- 历史不确定：沿用显式策略，默认阻断。

首次持久化执行仍通过 FirstRoundRecovery 安全入口，从已入账 Accepted Plan 开始。没有正式计划时，在任何阶段调用之前拒绝。没有新增绕过计划接受过程的“自动持久化普通请求”开关。

因此本次完成的是业务与 phase 机制收敛，不是所有外部调用入口收敛；后者仍属于 8D-3。

## 4. 保留既有错误语义

普通兼容入口此前将非法 candidate 交给 7A，得到 REJECTED。持久化路径此前在保存 Query 请求前校验，非法 candidate 被保存为 QUERY_FORMULATION_INVALID 的 FAILED 结果。

两种语义本次均保留，差异限定在阶段适配器：

- direct_phase 不提前改变 7A 的候选请求拒绝行为。
- ledger executor 只保存合法请求，或保存已有协议允许的 Query 失败事实。

Query 失败不伪造 Knowledge Execution Operation，不执行检索。失败和拒绝结果可以 COMMITTED，但不变成业务成功；零命中仍是 SUCCEEDED。

## 5. 完整性和权限

统一任务循环保留：非空计划、PENDING 状态、唯一 task_id、顺序执行、取消传播、结果 task_id/capability 一致、本轮 execution_id 唯一。

检索 produce 返回前就验证 TaskExecutionResult 身份，避免适配器先提交错误结果、节点之后才发现身份错配。Observation 在进入阶段执行器前校验完整输入，解释实现仍使用原来的 quote/source 完整性规则。

graph.py 在调用共享节点前将 JSON planning_input 转回 InitialPlanningInput，再校验 canonical 身份。恢复输出通过 wire 转成 JSON；普通输出保留类型化对象。

可信 Runtime 决定是否提供 ledger executor，State 不携带执行器或恢复策略。每任务和每个持久化阶段仍检查当前权限；_phase 中保留 READ + CONTINUE、运行锁与 generation 检查。

## 6. 三个事务保持原样

```text
事务 1：登记操作、查历史、复用或结算旧 STARTED → 提交
事务 2：检查重试策略、新建 STARTED、绑定 run/generation → 提交
事务外：produce() 调用共享业务逻辑
事务 3：Result + COMMITTED + Selection → 一起提交
```

没有长事务包住模型调用，没有覆盖旧 Attempt/Result，没有改变旧操作摘要或自动选择“最新成功”。锁丢失、UNCERTAIN 和显式重试继续依赖原恢复机制。

## 7. 测试与保留边界

新增测试覆盖：统一阶段顺序、Query 失败不创建检索阶段、错误任务身份不能提交、持久化首次执行确实使用共享循环、恢复复用原结果、非法 Query 失败稳定保存、缺少 Accepted Plan 不执行任何阶段。

原普通执行/Observation/Runtime 和真实 PostgreSQL 恢复测试继续回归，包含真实 os._exit、连接丢失 fencing、并发与原 serving snapshot 保留。

本次实际验证：普通执行/Observation/Runtime/Graph focused 65 passed；新增阶段单元测试 3 passed；
最终 backend 全量（启用真实 PostgreSQL）509 passed、7 skipped，1 条已有依赖弃用警告。
全量包含 25 个恢复集成用例及真实 os._exit 测试；未调用付费模型 API。git diff --check 通过。

P2-10/12/13/15/16 不因这次重构自动关闭。普通 Runtime 包装兼容仍保留；旧 checkpoint v1 仍明确拒绝；run-wide retry policy 和 hung-worker takeover 未扩展。

代码阅读顺序：graph.py → nodes.task_execution / observation → recovery.ledger_phase_executor → recovery._phase。
