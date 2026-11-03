# Stage 7B — 首轮知识任务执行

## 职责

只做 Query Formulation、顺序调用 Stage 7A、任务与执行结果关联并写回 LangGraph State。
7B 本身不实现 Observation、Hypothesis、Evidence、真实 Replanning、并行 DAG、自动重试、持久化或通用 Registry。
随后独立的 [7C Structured Observation](structured-observation.md) 已接入解释节点，7B 执行边界保持不变。

```text
InitialPlanningInput
    ↓ InvestigationAgent.initial_plan()
InvestigationPlan.tasks
    ↓ 每个知识任务独立 Query Formulation
candidate KnowledgeSearchToolRequest
    ↓ Stage 7A 校验、Scope、鉴权、Retrieval、Context Resolution
TaskExecutionResult
    ↓ 更新计划中的对应任务状态
task_execution_results 写回 State
    ↓ 现有占位 Observation/Replanning，仅结束本轮
round_complete=True，investigation_status=OPEN
```

## Query Formulation

同一个 InvestigationAgent 新增 formulate_knowledge_query，不拆 Query Agent。
输入包含原始调查上下文、计划 goal、当前 objective/expected_result 和 max_top_k。
模型只返回 query/filters/top_k，不能提供 actor、task_id、execution_id、profile 或物理 collection。

提示词要求根据目标提出具体查询，任务目标不直接当作 query；没有本地回退为 objective 的代码。
不能保证模型不会偶然生成与目标相同的文字，但每次必须实际经过独立查询构造和校验路径。

只接受完整 finish_reason=stop 的结构化响应；非法、空响应、截断或模型异常不会退回目标原文继续检索。
可信范围的最终约束仍由 7A 执行，模型的 filters 不是权限依据。

## 可信注入与调用方式

```python
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.recovery import FirstRoundRecovery

runner = FirstRoundRecovery(
    investigation_agent,
    session_factory=sessions,
    checkpointer=saver,
)
# Investigation / Round 已入账；prepare_plan 在图内生成候选、校验并接受。
result = await runner.ainvoke(context=InvestigationInvocation(runtime=runtime_context))
```

8B 已将原构图绑定身份改成每次调用提供 runtime_context，不设置默认身份，不从 State 读取模型声明的权限。
调用方负责认证操作者并从 canonical 调查记录建立 context；完整示例见[8B 运行上下文](investigation-runtime.md)。
profile 不由模型候选决定；读历史不代表可以继续执行。

异步工具节点使用 ainvoke，不再使用旧 skeleton 的同步 invoke。Initial Planning 在鉴权后调用原同步 Agent；
执行节点中的同步 Query Formulation SDK 放入 asyncio.to_thread，避免阻塞事件循环。

## State 与任务状态

State 增加 task_execution_results，保留原始 Child、Context、snapshot、实际请求和错误，不压成一段 prose。
Initial Planning 重置旧结果和 round_complete；7B 用本轮完整结果列表替换，不累加历史轮次。

正式任务要求非空、ID 非空且唯一、全部 PENDING；这不是恢复/重放入口，不能拿已执行计划静默重试。
不原地修改调用方或 Agent 返回的计划；通过新计划对象返回状态更新。

| 执行结果 | 正式任务状态 | 说明 |
|---|---|---|
| SUCCEEDED，含命中 | COMPLETED | 工具链执行成功，不证明当前根因成立 |
| SUCCEEDED，零命中 | COMPLETED | 执行完成但缺少知识 |
| FAILED | FAILED | 错误类别与 retryable 保留 |
| REJECTED | FAILED | 既有 TaskStatus 无 REJECTED，拒绝细节保留在执行结果 |

每个结果校验 task_id、capability 与当前正式任务一致；execution_id 本轮唯一。
异常身份或不合法返回值直接中止本次节点，不能把错配结果写入 State。

## 顺序与错误策略

任务按计划顺序执行，每个任务的 Query Formulation、7A 完成之后才开始下一个任务。
已知普通失败/拒绝记录为结构化结果，继续后续独立任务；现有任务契约没有依赖关系，不自行推断依赖。

查询构造失败没有调用知识工具，但系统生成一条尝试记录，代码清楚标为 QUERY_FORMULATION_*：

- TIMEOUT：DEPENDENCY，可重试。
- INVALID：VALIDATION，不可自动重试。
- UNAVAILABLE/API_FAILURE：DEPENDENCY，依据故障类型给出 retryable。
- FAILED：INTERNAL，不把原异常正文暴露给 Agent。

非知识 capability 不执行 Query Formulation 或其他工具，交给 7A 明确拒绝。
外部取消继续传播；不自动产生一个“取消后成功”的结果。

## 时间、恢复与成本边界

查询构造使用可信 timeout_seconds；随后 7A 用自己的同一配置控制 Retrieval+Context Resolution。
这是两个阶段各自的预算，不是整个任务共用一个绝对截止时间。

查询构造失败记录的时间覆盖查询尝试；成功调用的 7A 时间仍只代表工具执行，不擅自改变已封板结果语义。
7B 尚未新增查询 token/耗时审计、模型版本快照，不能把它宣称成完整费用报表。

线程中的同步 SDK 不能被 asyncio timeout 强制终止，上游可能继续消耗资源；SDK 自身也可能重试。
本层无自动重试，并不保证所有外部请求恰好一次或完全停止。

执行节点完成后一次写回本轮列表；若中途取消、进程崩溃或程序完整性错误，已经发生的外部请求
可能没有写回 State。Stage 8 才设计持久化/恢复/幂等，不能把当前做成生产恢复承诺。

## 测试

- Query Formulation 的提示/响应、额外可信字段拒绝、截断与非法 JSON。
- 两个正式任务按顺序运行且 query 不是 objective 回退。
- 真实 7A 适配器路径（仅替换 RAG 依赖），验证 scope 与授权。
- 零命中、拒绝、查询构造失败、解析完整性失败的状态与关联。
- 查询超时不调用检索；外部取消传播。
- 任务 ID 重复、结果身份错配阻止写入 State。
- 完整 Graph 执行后 round_complete=True，但案件仍 OPEN，无 Observation/Hypothesis。

测试替身不等于真实模型效果。本阶段不自动调用真实模型或生产数据库来证明查询构造质量。

## 保留后续问题

Query Formulation 应后续单独评估；可信 scope 仍只覆盖现有 KnowledgeFilters；execution 仍使用私有 scalar；
执行记录还没有稳定 tool_name；案件状态暂时只允许 OPEN。此次均不为未来需求扩展 7A 契约。
