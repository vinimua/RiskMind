# Stage 7A — Execution Contracts + Knowledge Tool Adapter

## 范围

只实现 `execution/contracts.py` 与 `execution/knowledge.py` 的受控知识调用，不接 LangGraph、不实现 Observation、Query Agent、Registry、DAG 调度或 Celery，不修改数据库、RAG schema 和已封板策略。

```text
可信 InvestigationTask + 候选 KnowledgeSearchToolRequest
    ↓ 输入校验 / 可信 Scope 收紧
可信应用鉴权器显式授权
    ↓
retrieve_dense_children → resolve_knowledge_contexts
    ↓ 输出快照/Child/Context 关联校验
TaskExecutionResult
```

Task 表达目标，不直接当作 query。Query Formulation 由未来调用层形成候选请求；7A 不生成查询，也不改变已有 Investigation Agent。

## 契约

- KnowledgeSearchToolRequest：query、filters、严格正整数 top_k；拒绝 execution_id、actor_id、profile、collection 等额外字段。
- KnowledgeExecutionContext：可信应用提供 actor_id、index_profile、scope、max_top_k、timeout_seconds，不接受模型声明权限。该模型本身不能验证调用方身份，身份必须由外层认证边界建立。
- KnowledgeExecutionPayload：保留实际生效请求、DenseChildRetrievalResult 和 ContextResolutionResult，包括原 route snapshot、Child Hit、分数和 Context supporting_hits。
- TaskExecutionResult：系统生成 execution_id、可信 task_id/capability、status、带时区的起止时间、payload 或 error。
- ExecutionError：category、code、中文 message、retryable。

状态只有 SUCCEEDED / FAILED / REJECTED。零命中是 SUCCEEDED，载荷中的 hits/contexts 为空，没有 EMPTY 状态。

成功必须有 payload 且无 error；失败/拒绝必须有 error 且无部分 payload；完成时间不得早于开始时间。

## 调用约定

调用 `execute_knowledge_search(task, candidate_request, context=..., authorize=...)`。
authorize 是可信应用的同步回调，参数为 actor_id、`knowledge:search`、可信 context，必须显式返回 True。
不能从模型提供的布尔值构造回调，也不能仅因为请求包含 actor_id 就认定身份可信。

candidate_request 可为字典或 KnowledgeSearchToolRequest，必须重新校验。系统 Task/context 非法属于调用方程序错误，直接报错，不用伪造 task_id 产生“可信”结果。

session_factory、embedding_client、milvus_client 可注入，主要用于测试和明确环境选择，不由模型参数控制。

## Scope Enforcement

可信 scope 中 document_id/model_id/document_version 非空时：候选未提供则补齐，候选不同则拒绝。
可信 document_types 非空时：候选可以选择其中子集；候选为空则采用可信集合；超出允许集合则拒绝。
top_k 超过系统 max_top_k 则拒绝，不悄悄改写请求数量。

scope 为空表示该层没有附加限制，不代表自动完成租户鉴权。7B 的应用入口必须按案件、身份构造可信 scope 与权限策略。

## 结构化结果与错误

| 场景 | 状态 | 类别 / code |
|---|---|---|
| 正常命中或零命中 | SUCCEEDED | 无错误 |
| 非知识 capability | REJECTED | VALIDATION / UNSUPPORTED_CAPABILITY |
| 候选非法、越界 | REJECTED | VALIDATION / INVALID_TOOL_REQUEST |
| 权限未显式通过 | REJECTED | AUTHORIZATION / TOOL_NOT_AUTHORIZED |
| 鉴权器异常 | FAILED | INTERNAL / AUTHORIZATION_CHECK_FAILED |
| 工具链超时、上游超时 | FAILED | DEPENDENCY / KNOWLEDGE_SEARCH_TIMEOUT |
| 检索/解析/输出契约不一致 | FAILED | INTEGRITY / KNOWLEDGE_INTEGRITY_FAILURE |
| 缺 route | FAILED | DEPENDENCY / SERVING_ROUTE_MISSING |
| Embedding API 状态错误 | FAILED | DEPENDENCY / EMBEDDING_API_FAILURE |
| 网络、数据库、Milvus 异常 | FAILED | DEPENDENCY / KNOWLEDGE_DEPENDENCY_FAILURE |
| 未预期异常 | FAILED | INTERNAL / KNOWLEDGE_EXECUTION_FAILURE |

上游状态 429/5xx、超时与依赖访问失败可标 retryable；该标志只是调用方处理建议，不会在此自动重试。
数据库/Milvus 异常当前按较粗粒度依赖类别归档，调用方仍需排查配置等永久故障，不能无限循环重试。

错误不回传原始异常正文，防止泄露 DSN、凭据和响应内容。外部取消继续传播，不吞成失败或成功。

## 输出校验

两个阶段 snapshot 必须相同且 profile 等于可信 context；Child ID 唯一、排名连续、数量不超过请求 top_k；所有 Context 的支持 Child 必须恰好分配完整命中，且载荷一致。

正常适配器继续依赖已封板 Retrieval 的逐字段 PG 校验与 Context Resolution 的来源/页码检查，不复制另一份算法。

## 当前保障与后续边界

- execution_id 在执行边界按 UUID 生成，是一次尝试身份，不是持久化幂等键；重复调用会得到不同身份。
- 当前返回执行事实，但不落库，不能声称已具备持久化审计、崩溃恢复或 exactly-once。
- 本层不自动重试；SDK 可能有自己的重试行为，Embedding 可能重复计费。
- asyncio.timeout 覆盖检索与解析，不能撤销已发出的上游请求，也不能强制终止同步 Milvus 线程。它不是硬实时终止保障。
- 不更新 InvestigationTask.status，不修改 Graph State。
- investigation_complete 的 V0 临时语义留待 7B 进入之前修正，本次不接 Graph。
- 不实现任何 Observation 数据模型；7C 必须验证 execution_refs 属于当前 round 的真实执行结果。
- Query Formulation 质量与 Observation 模型/提示版本追踪是后续评估项。

## 后续顺序

7B：顺序执行正式知识任务，接入 State，区分 round 完成与 investigation OPEN。
7C：确定性检查 + Investigation Agent 解释 + 引用校验。
Stage 8：持久化、恢复、幂等、可信身份关联。
Stage 9：多轮重规划。

先 review 7A，再推进后续；不把上述阶段合并进本次提交。
