# 通用记忆契约与上下文解耦（历史设计）

> 本文记录上一版逐条来源结构。当前已进一步简化为分类正文和集中来源，代码及迁移以 [记忆模块审阅指南](investigation-memory-design.md) 为准。

## 目标调用链

```text
调查账本 → 案件授权和来源校验 → InvestigationMemoryItem → context_entries()
                                                                  ↓
                                                         ContextEntry 候选
                                                                  ↓
未来消息库 → 会话授权和来源校验 → ConversationSource + MemoryItem → 共用 assemble_context()
                                                                  ↓
                                                    AgentContext → 模型输入
```

本次实现的是通用内容、类型化来源、归属和共用上下文组装。案件写入、来源重验与阶段快照继续走原持久化调用链。没有新增聊天数据库、聊天 API、会话授权服务、自动摘要或通用记忆检索服务。

## 契约分工

- `MemoryItem`：通用性质与正文，允许偏好、背景、决策、建议等；不要求 execution_refs，也不携带调查 ID。
- `InvestigationMemoryItem`：案件专用存储条目，要求执行引用；保留现有案件记忆的持久化形状，不重写旧事实。
- `CaseMemory`：仍是 Observation 派生记录，保持案件、轮次和结果来源的严格关联。
- `InvestigationSource`：调查来源，明确案件、轮次、结果、Observation 和执行引用。
- `ConversationSource`：聊天来源，明确 conversation_id 与 message_ids；不能混入 observation_id 等字段。
- `ContextEntry`：memory_id + item_index + source + item + clipped。来源集中在 source，item_index 精确定位原记忆中的条目。
- `AgentContext`：scope 为 InvestigationScope 或 ConversationScope，purpose 支持 CHAT_RESPONSE；不再强制有案件。
- `ContextSnapshot`：固定某次运行/阶段的实际输入，格式升级为 v2。

通用契约和组装器不再导入 workflow/execution。这里的 Identity 只做字符串约束，不自动生成 ID、不声明 FK、更不赋予权限。

## 案件与聊天使用同一个组装器

调查适配器先核对权限和已提交来源，转换为通用 ContextEntry 后组装。聊天侧可以使用如下契约（示意，消息持久化和授权仍需实现）：

```python
entry = ContextEntry(
    memory_id="memory-chat-1",
    item_index=0,
    source=ConversationSource(
        conversation_id="conversation-1",
        message_ids=("message-1",),
    ),
    item=MemoryItem(kind="PREFERENCE", content="用户希望回答简洁"),
)
context = assemble_context(
    scope=ConversationScope(conversation_id="conversation-1"),
    purpose="CHAT_RESPONSE",
    candidates=(entry,),
)
```

不需要为聊天伪造执行、案件、轮次或 Observation。来源是调用方核验后的业务数据，不能直接接受浏览器或模型伪造的来源对象。

## 权限和合法组合

组装器不查询数据库或判断用户权限。调用方必须先执行对应来源的授权与事实核验。组装器仍验证所有候选的归属，即使候选最终会因为预算被省略，也不能掩盖跨案件、跨会话条目。

当前一个上下文只属于一个案件或会话。会话与案件的关联授权尚未实现，因此禁止直接混装两种来源；未来在可信关联规则落地后扩展，不通过“把全部字段改成 None”绕过。

EXECUTION 只能来自调查来源；聊天中的“工具成功了”不能据此成为正式执行事实。HISTORICAL_FINDING 必须包含 limitation。truncated 必须与遗漏数及条目 clipped 一致，同一 memory_id + item_index 不得重复。

## 持久化和恢复

CaseMemory 继续按 v1 载荷保存，包含原有的 schema_version 与 extraction_version，避免本次解耦顺便覆盖历史来源。两者本次未实现独立多版本提取算法。

AgentContext / ContextSnapshot 使用 v2 形状，嵌套小对象不再各自添加 schema_version。新迁移 0010 仅放宽快照表版本约束以容纳 v2；旧 v1 数据原样保留，但读取会明确拒绝，不自动映射或重写。

新案件快照的恢复：核对运行、阶段、scope、用途及摘要；重新授权来源记忆；按 item_index 重建原条目，校验来源与正文（含明确裁剪的前缀）。来源不符即拒绝，不允许快照中混入聊天来源绕过案件读取规则。

快照表仍由案件仓库写入，数据库列 investigation_id 保持必填：它是案件持久化适配，不是已实现的通用聊天快照存储。未来聊天有独立消息/会话生命周期，接入时需要实现存储和权限，不能只拿这张表强塞 conversation_id。

## 审阅入口

1. [contracts.py](../backend/src/risk_platform/memory/contracts.py)：通用内容、来源 union 与合法组合。
2. [context.py](../backend/src/risk_platform/memory/context.py)：无调查字段读取的共用预算组装。
3. [investigation_memory.py](../backend/src/risk_platform/memory/investigation_memory.py)：案件提取与专用适配。
4. [repository.py](../backend/src/risk_platform/memory/repository.py)：案件权限、快照版本和原条目核验。
5. [0010 迁移](../backend/alembic/versions/20261110_0010_generic_memory_context.py)：不修改旧载荷，有 v2 时拒绝降级。
6. [通用测试](../backend/tests/workflow/test_generic_memory.py)、[案件单元测试](../backend/tests/workflow/test_case_memory.py)、[案件 PG 测试](../backend/tests/workflow/test_case_memory_postgres.py)。

## 验证范围

通用契约及组装的单元测试覆盖聊天偏好、来源隔离、非法来源组合、裁剪、限制条件、覆盖一致性、重复条目和快照摘要。案件 PG 测试已适配新格式，并新增旧快照保留但拒绝读取场景。

本次 Docker 引擎未运行，真实 PG 回归尚未重新执行；不能引用上次的 3 passed 作为本次结果。离线迁移 SQL 生成通过，只证明 Alembic 能生成 SQL，不替代真实数据库约束测试。未将迁移应用到业务数据库。


本次实际执行结果：默认 backend 测试 536 passed、108 skipped；记忆专用单元测试 18 passed；一条已有 LangChain 弃用预告。`git diff --check` 和文档链接检查通过。未执行新的真实 PG 测试、未应用业务迁移、未提交 Git。
