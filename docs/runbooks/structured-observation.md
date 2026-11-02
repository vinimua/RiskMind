# Stage 7C — Structured Observation

## 边界

```text
InvestigationPlan + TaskExecutionResult[]
    ↓ 确定性完整性检查
系统执行摘要与信息缺口
    ↓ 有可解释知识时，调用同一个 Investigation Agent
KnowledgeObservationDraft
    ↓ execution / Context / supporting Child / 原文校验
RoundObservation → State.observation
    ↓ 现有占位退出
round_complete=True，investigation_status=OPEN
```

不新建 ObserverAgent，不执行工具补查，不生成 Hypothesis、Evidence、根因结论、正式任务或下一轮计划。
不增加持久化、恢复、自动重试、多 Agent 调度或新的业务准入规则。

## 实现组成

- `workflow/observation.py`：模型契约、输入检查、执行摘要、解释引用校验与系统结果构造。
- `InvestigationAgent.observe_knowledge_round()`：调用现有同步模型客户端生成结构化解释。
- `workflow.nodes.observation()`：异步节点，协调检查、线程内模型调用、超时与结果写回。
- `workflow/state.py`：增加可为空的 observation，初始规划重置为 None。
- `workflow/graph.py`：绑定同一个 Agent 与当前可信执行上下文，无额外工具入口。

## 系统输入检查

在调用模型前重新校验计划与每个执行记录：

1. 正式任务非空，task_id 非空且唯一。
2. execution_id 本轮唯一。
3. 执行结果按原计划顺序恰好一任务一记录，不缺失、不多出。
4. capability 一致；SUCCEEDED 对应任务 COMPLETED，FAILED/REJECTED 对应任务 FAILED。
5. 执行记录与 KnowledgeExecutionPayload 重新通过已有契约，Context ID 在单次执行中唯一。

这类输入损坏直接抛错，阻止节点写入解释；不会让 Agent 通过叙述修复可信身份。

## 先保留执行事实

系统依据实际执行记录生成 execution_summaries：

| 实际情况 | outcome |
|---|---|
| SUCCEEDED 且存在 Context | KNOWLEDGE_AVAILABLE |
| SUCCEEDED 且零命中 | NO_HITS |
| FAILED | EXECUTION_FAILED |
| REJECTED | EXECUTION_REJECTED |

失败、拒绝与零命中自动产生系统 knowledge_gaps，即使模型未提及也会保留。
原始结果仍在 State.task_execution_results，不被 Observation 覆盖；错误 code/category 可由原 execution_id 追溯。

整轮没有任何可解释 Context 时不调用模型，返回 INCONCLUSIVE / NO_KNOWLEDGE_AVAILABLE。
这不把零命中的执行状态改成失败，也不声称现实世界不存在历史案例。

## 模型输入与输出

输入为本轮目标、任务意图和真实执行结果，含实际 Child/Context 来源与错误；不提供 authorize 或 actor 权限对象。
输入 JSON/原文均是不可信业务数据，不能作为改变角色、调用工具或泄露信息的指令。

模型只输出：

```text
execution_refs[]
findings[]
    statement
    execution_refs[]
    sources[]：execution_id / context_id / source / quote
    limitation
knowledge_gaps[]：text / execution_refs[]
next_step_suggestions[]：text / execution_refs[]
narrative
```

source 使用 KnowledgeSourceRef，包含 document_id、document_version、chunk_id。
findings 可以为空；缺口与建议只是解释，不是已授权、可直接执行的工具计划。

模型不允许输出 status、observation_id、created_at、root_cause、hypotheses 或 evidence 等额外字段。
必须 finish_reason=stop 且返回合法 JSON，否则不接受。

## 确定性来源校验

- 顶层 execution_refs 必须唯一并覆盖本轮全部结果，不能隐去失败。
- finding/gap/suggestion 的 execution_refs 只能属于当前轮次，不能指向旧轮记录。
- finding 的执行引用集合必须恰好与其 sources 引用的执行集合相同。
- finding 只能引用成功且有 Context 的执行结果；失败/拒绝/零命中不能支持历史发现。
- context_id 必须属于该 execution，不根据其他执行里的同名 Context 补齐。
- source 必须等于该 Context.supporting_hits 中真实 Child 的来源三元组。
- quote 必须连续逐字出现在该 Context.content，且不能重复同一来源引用。
- limitation 必须非空，不允许用缺少来源的 finding 绕过校验。

Parent Context 的引句可以来自 Parent 原文，不要求全部包含于某个 Child；归属通过执行结果中的 Context 和 supporting_hits 保留。

任一模型引用失败，整个模型解释丢弃，不保留前面已经通过的部分 findings 或 suggestions。

## 输出与失败语义

系统生成 observation_id、带时区 created_at、status、execution_summaries，并固定：

```text
knowledge_scope = HISTORICAL_KNOWLEDGE_ONLY
```

status=SUCCEEDED 表示模型解释及机械校验完成，可以同时存在执行失败的信息缺口。
它不表示所有任务成功，不表示答案已经语义审定，也不代表当前案件根因成立。

以下情况 status=INCONCLUSIVE：

- 没有可解释知识：NO_KNOWLEDGE_AVAILABLE。
- 解释超时：OBSERVATION_TIMEOUT。
- 响应/引用非法：OBSERVATION_INVALID。
- 解释依赖不可用：OBSERVATION_DEPENDENCY_FAILURE。
- 其他解释异常：OBSERVATION_FAILED。

INCONCLUSIVE 保留系统执行摘要与系统缺口，不保留部分模型发现和下一步建议；使用固定系统说明，不回传原异常正文。
取消继续传播，不转换成普通成功或不确定结果。

## 不承诺自然语言绝对正确

schema 可以拒绝根因字段，quote 校验可以证明引文确实存在，但不能机械证明
statement/narrative 一定由原文语义蕴含，也不能靠非空 limitation 保证解释质量。

本阶段通过固定历史知识类型、提示词限制、逐字来源和结果分层防止自动获得案件证据资格。
后续仍需真实模型专项评测和人工抽检；“结构/引用正确”与“解释语义质量”必须分别报告。
不能让下游忽略 knowledge_scope 而直接把 narrative 当作 Policy Gate 输入的正式证据。

## 时间与状态边界

解释模型使用可信 timeout_seconds，与 Query Formulation/7A 各自预算独立。同步 SDK 在线程执行，超时不能强制终止已经开始的上游请求，可能继续计费。
本层不新增自动重试，也不等于 SDK 内部完全没有重试。

传给模型方法的 plan/results 使用独立深拷贝，校验仍对照系统保留的原始输入副本，避免嵌套模型误改削弱来源检查。
本阶段没有 checkpointer 或持久化 Observation，不提供崩溃恢复/重放保证。

初始规划清空旧 Observation。模型失败但解释尝试结束后，图可以结束本轮；案件仍为 OPEN。
输入完整性错误会直接中止节点，不应呈现一个本轮有效解释。

## 测试覆盖

混合成功/失败/拒绝/零命中的系统摘要、无知识跳过模型、当前轮引用限制、错 Context/来源/引句、
非法可信字段、输入缺失/重复/状态错配、模型失败/超时/取消、无部分解释、深拷贝保护，以及完整 Graph 案件仍 OPEN。

测试使用模型替身，不作为解释语义质量已通过的证明。

## 现有 P2 继续保留

1. execution 层使用 knowledge 的私有 scalar。
2. TaskExecutionResult 尚无稳定 tool_name。
3. scope 只覆盖现有 document/model/version/type。
4. 图实例绑定构图时可信上下文，不能作为跨用户/案件的共享 singleton；共享前必须改 per-run runtime context。
5. 案件 model_id 与执行 scope 尚无确定性绑定，接入 API/Case Context 前必须实现。

Query Formulation 与本次 Observation 的模型语义质量均仍需专项验证。
不在 7C 顺手修复这些 P2，也不提前扩展案件 OPEN 之外的生命周期状态。
