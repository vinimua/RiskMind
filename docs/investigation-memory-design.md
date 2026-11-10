# 记忆模块：简化后的代码与审阅指南

## 本次调整

参考 DevF 按列表组织正文的做法，删除 MemoryItem、InvestigationMemoryItem、CaseMemory、ContextEntry 和多组来源子类。核心数据变成五类，另有一个共同校验基类：

```text
MemoryScope：kind + id，属于案件还是会话
MemoryContent：按列表保存正文
MemoryRecord：一份来源 + 一组分类内容
AgentContext：本次选中的 MemoryRecord + 预算
ContextSnapshot：把 AgentContext 固定到运行/阶段
```

不再为每句话保存 kind、版本、item_index、execution_refs 和完整来源。分类由列表名称表达，来源每份记忆集中保存一次；需要精确核查时读取原始来源。这里没有实现独立条目的编辑、撤销或稳定 item_id。

## 数据示例

```python
MemoryRecord(
    memory_id="memory-chat-1",
    scope=MemoryScope(kind="CONVERSATION", id="conversation-1"),
    source_type="CHAT_MESSAGE",
    source_id="message-1",
    content=MemoryContent(
        user_preferences=("请用中文简短回答",),
        follow_ups=("下一步补充集成测试",),
    ),
)
```

目前五组内容为 execution_summaries、historical_findings、follow_ups、user_preferences、background；不再保留 tasks、open_questions、decisions 三个重复分类。它们只是内容性质，进入列表不等于内容已证实。聊天来源不能填写正式执行摘要；聊天存储、授权和 API 仍未实现。

## 当前调用链

```text
prepare_plan
  → context_for_stage：授权读取、固定原上下文
  → 模型生成候选、正式接受
任务请求生成
  → context_for_stage：固定任务上下文
  → 模型选择工具并形成参数
Observation 提交事务
  → Result + Attempt.COMMITTED
  → extract_memory：确定性分类提取
  → capture_observation：保存 MemoryRecord
  → Selection
  → 原子提交
```

图拓扑、运行控制和工具调用入口没有增加第二份实现。外部模型调用仍在事务外，跨调用的鉴权与 fencing 保留。

## 各文件职责

| 文件 | 职责 |
| --- | --- |
| [contracts.py](../backend/src/risk_platform/memory/contracts.py) | 五个核心数据模型和共同约束 |
| [investigation_memory.py](../backend/src/risk_platform/memory/investigation_memory.py) | 直接从 Observation 生成分类内容，删除逐条适配函数 |
| [context.py](../backend/src/risk_platform/memory/context.py) | 案件/聊天共用组装，无 workflow/execution 依赖 |
| [repository.py](../backend/src/risk_platform/memory/repository.py) | 案件专用来源验证、授权、幂等与恢复 |
| [models.py](../backend/src/risk_platform/memory/models.py) | 案件派生记忆和阶段快照的物理表 |

数据库现有 case_memories 和 agent_context_snapshots 仍是案件存储适配，不冒充聊天数据库。通用的是内容、记录契约和组装逻辑，不是尚未实现的消息存储或检索服务。

## 来源与事实

案件 source_type 为 OBSERVATION_RESULT，source_id 指向账本 Result。由 Result 读取原 Observation，再追到 execution_refs、工具结果及知识引用。轮次和案件在数据库列及账本中核对，不在每条正文中重复存储。

执行摘要保留 execution_id、task_id、status、outcome；发现与原 limitation 合并为一条字符串。缺口和下一步建议统一进入 follow_ups，保持原顺序并对完全相同的正文去重；这里只是记录建议，不会生成正式调查任务或审批决定。

提取时仍核对 finding/note 的执行引用属于源 Observation。召回时按来源轮次核验当前权限、知识工具独立授权、支撑结果范围，再重新提取并比较整份记忆。来源存在不等于自然语言结论被证明。

## 预算策略更简单

候选须先由来源适配器授权并排序。组装器先核对所有候选的归属及重复身份，然后按顺序选择完整正文。

- 正文能放入预算：整条保留。
- 不能放入：整条省略。
- 不再截取前 2000 字，不再维护每条 clipped。
- 不保存省略计数和重复布尔标记；模型数据区明确提示所选记忆不是完整历史。
- 默认 12000 字符覆盖序列化上下文，不是 token 数或最终完整 Prompt 上限。

这样不会把“结论 + 限制”截成半句话，但可能完全省略一条很长内容；通过通用提示声明所选内容并非全量，原始内容仍可回查。语义召回、token 预算、自动摘要、滑动窗口及原文回查工具尚未实现。

## 快照恢复

快照保存实际使用的分组内容及来源。恢复时重新授权原记忆，并检查快照只能从各分类列表中保持顺序地整条省略；不能改写、重排或重复正文，也不能替换 source_id。恢复不重新按最新历史或预算选择内容。

记忆与 Observation 同事务保存仅适用于当前本地确定性提取。若以后引入 LLM 摘要，应独立入账，不能让提交事务等待外部模型。当前派生写入失败仍导致 Observation 提交事务回滚，不伪造已提交结果。

## 格式与迁移

新 MemoryRecord 载荷 v3，新 AgentContext / ContextSnapshot v6。旧载荷原样保留，新代码明确拒绝读取旧格式；没有旧类、自动投影分支或覆盖旧记录。已有旧运行的恢复需要单独处理，不支持透明滚动升级。

[0014 迁移](../backend/alembic/versions/20261110_0014_remove_memory_statistics.py) 仅扩展版本约束，不修改历史 payload。有新格式数据时拒绝降级。迁移链继续保留，不删除数据库记录。

## 后续完整架构边界

- 原始历史：调查复用账本，未来聊天增加消息/封存会话存储。
- 会话记忆：可使用同一 MemoryRecord / MemoryContent，必须有真实消息来源与会话授权。
- 检索投影：未来可重建资料索引，不作为事实权威来源。
- 渐进压缩：以后增加去重、窗口和版本化摘要，保留原文回查及覆盖范围。
- 长期知识：经验候选→审核→入库→撤销，不能由 Observation 自动升级为已确认事实。
- 聊天行动请求：经过意图与权限校验，再进入唯一持久化调查入口；跨案件/会话混装仍未实现。

这些未来模块没有创建空实现文件，也不能称为已完成能力。

## 建议审阅顺序

contracts.py → extract_memory → assemble_context → capture_observation / context_for_stage → 节点接入 → 0014 迁移。

测试入口：[案件单元测试](../backend/tests/workflow/test_case_memory.py)、[共用组装与恢复核验测试](../backend/tests/workflow/test_generic_memory.py)、[真实 PG 集成测试](../backend/tests/workflow/test_case_memory_postgres.py)。重点核对来源集中后仍可追溯、非法跨范围输入拒绝、限制不被截断、重复保存与恢复不会重做外部调用。


## 上一次结构简化验证（历史记录）

- 默认 backend 测试：534 passed、108 skipped。
- 记忆单元测试：16 passed（包含在默认测试中，不重复累计）。
- 真实 PostgreSQL 记忆、初始规划及恢复回归：48 passed，使用 UUID 临时 schema；模型/检索使用替身，不产生 API 费用。
- 一条已有 LangChainPendingDeprecationWarning；格式和文档链接检查通过。
- 迁移仅在隔离测试 schema 验证，未应用到业务数据库；测试 PostgreSQL 已恢复停止状态，其他容器未修改。
- 未提交 Git。真实模型的记忆召回效果和语义质量没有因此得到证明。


## 合并后续事项

删除 decisions，合并 tasks 与 open_questions 为 follow_ups。正式决定仍由计划、审批等业务记录保存；记忆不维护第二套决定。不同表达但语义相近的事项不自动合并，避免丢失原问题或建议。预算依旧整条省略，不截断正文。

本次格式有新版本，0012 只扩展数据库版本约束，不覆盖旧载荷；旧记录或旧快照需要显式处理，不能透明恢复为新输入。旧类、字段别名及自动转换分支均未保留。


本次合并字段验证：默认 backend 测试 538 passed、108 skipped；记忆真实 PG 测试 4 passed，迁移在 UUID 隔离 schema 验证。已有一条 LangChain 弃用预告。业务数据库未迁移，测试容器恢复停止状态，未提交 Git。


## 删除冗余用途字段

AgentContext 不再保存 purpose，assemble_context 和 context_for_stage 也不再接收该参数。模型材料只描述归属、实际记忆和覆盖/预算。持久化阶段使用已有 run_id + stage 定位，恢复校验运行、阶段和 scope。快照格式升级 v5，记忆正文格式仍为 v3；旧快照不自动改写。正式入口的权限及 fencing 未删除。

删除 purpose 的验证：默认测试 539 passed、108 skipped；真实 PG 记忆测试 4 passed；一条已有依赖弃用预告。仅隔离测试 schema 应用迁移，业务数据库未变更，测试容器恢复停止状态。


## 删除未使用的覆盖统计

已删除 omitted_count、truncated 及其一致性检查；不再为它们维护计数。组装器仍执行字符预算和整条选择。模型输入明确提示“所选记忆不是完整历史”，但没有精确遗漏统计。快照格式 v6，旧载荷保持不变、不自动转换。

移除覆盖统计的验证：默认测试 541 passed、108 skipped；真实 PG 记忆测试 4 passed；一条已有依赖弃用预告。只在隔离 schema 验证迁移，未应用业务迁移，测试容器恢复停止状态。
