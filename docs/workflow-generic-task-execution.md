# 通用任务入口与工具专用执行边界

## 1. 本次修正的问题

此前 Runtime 已通用、工具也有注册，但 graph.execute_node 仍提前调用 knowledge_execution，
向 nodes.task_execution 传 KnowledgeExecutionContext 与 knowledge_authorizer。
这会让所有任务都先依赖知识工具。本次把这个前置条件从通用入口移除。

## 2. 新调用链

```text
graph.execute_node
  check_runtime（案件权限、规划输入和已配置绑定的合法性）
  nodes.task_execution(runtime_context=call.runtime)
    ↓
按任务取得可用工具交集：已注册 ∩ capability 匹配 ∩ 本次已绑定
    ↓
每个候选工具通过自身 model_input 适配器提供公开参数限制
    ↓
Agent.formulate_tool_call：一次模型调用，选择工具并提出参数
    ↓
ToolCall(tool_name, arguments)
    ↓
validate_tool_call：重新核对本次绑定，按专用 schema 和限制校验
    ↓
QueryOutcome 保留 tool_name / candidate，交给 phase executor
    ↓
按所选 ToolDefinition.execution_phase 进入阶段执行
    ↓
execute_registered_tool(selected.tool_name, ..., runtime_context=runtime)
    ↓
具体工具 handler
  knowledge_search → 取得 KnowledgeExecutionContext + knowledge_authorizer → 原 7A
```

图拓扑和任务顺序不变，没有另建编排器、Factory、Manager 或自动重试机制。

## 3. 职责划分

| 位置 | 负责 | 不再负责 |
|---|---|---|
| graph.execute_node | 通用 CONTINUE 与案件输入校验，传 Runtime | 提取知识配置、构造知识授权函数 |
| nodes.task_execution | 正式任务身份、顺序、工具选择、结果身份与状态 | 固定知识工具名、接收知识上下文 |
| Agent.formulate_tool_call | 公开工具声明与参数限制 → 原生调用 | 获取 actor、权限回调和物理 collection |
| ToolDefinition | 专用请求 schema、模型限制适配、请求限制、handler、阶段映射 | 授予用户权限 |
| _knowledge_handler | 获取知识上下文和独立授权，调用 7A | 编排其他任务 |
| access._bound_context | canonical 案件事实与通用 READ/CONTINUE 权限 | 默认要求知识绑定 |
| access._knowledge_bound_context | 当前知识账本读写范围适配 | 代表所有未来工具的账本协议 |

为生成知识工具的 max_top_k 提示，需要由知识适配器读取其绑定，这是候选工具自己的准备工作，
不是所有任务入口统一获取知识上下文。其他工具的准备适配器不必调用 knowledge_execution。

## 4. 通用 API

```python
await task_execution(
    state,
    investigation_agent=agent,
    runtime_context=trusted_runtime,
    execute_phase=phase_executor,
)
```

不再接受 execution_context=KnowledgeExecutionContext 和知识 authorize 参数。
observation 同样接收通用 runtime_context，并使用运行预算；它仍解释当前已实现的知识结果契约，
不是已经实现其他 capability 的 Observation。

formulate_knowledge_query 保留为独立知识专用构造接口，内部调用通用选择方法；
workflow 不回退调用它，测试替身也改为提供 formulate_tool_call。

## 5. 工具可见性、校验和授权

- 注册但未绑定：不出现在模型工具列表。
- 已绑定但未注册：不会成为可执行工具。
- 注册并绑定但能力不匹配：不出现在本任务列表。
- 模型选择未提供的工具：QUERY_FORMULATION_INVALID，不执行 handler。
- 没有可用工具：通用任务组件在调用模型前返回明确拒绝。
- 专用 scope/profile/actor 仍来自绑定，模型只能提供专用参数。
- 实际执行时重新检查通用权限与绑定，知识 handler 再取得当前知识上下文并执行知识授权。
- 同一轮顺序、零命中成功、FAILED/REJECTED 无部分 payload、取消传播保持原规则。

工具可见不等于 authorize 回调已允许本次调用：注册/绑定是可用范围，最终授权仍在执行边界。

## 6. 恢复与持久化边界

ToolCall 和 QueryOutcome 是内存适配产物，不直接作为通用新数据库 payload 保存。
现有 QUERY_FORMULATION 成功记录仍为专用 KnowledgeSearchToolRequest，
其不可变业务输入已包含 Literal[knowledge_search]。恢复时显式恢复此 tool_name，
不再由 nodes 无条件硬编码为知识工具。

现有 ledger_phase_executor 对其他工具名明确拒绝，不能将其写入 KNOWLEDGE_EXECUTION。
PhaseArtifact、AttemptAttribution、AcceptedPlan 仍保留当前知识阶段的支持范围。
最新边界已通过 0008 分离：运行只保存 RunIdentity，阶段模型协议由 Operation 保存，
规划输入单独版本化；FirstRoundSpec 仅供读取 v1 历史。详见执行模块指南。

因此：

```text
通用任务组件支持独立工具适配器
    ≠
当前持久化 runner 已支持计算/事实工具
```

未来新增工具仍须增加专用执行/结果契约及相应账本版本，不能只增加一条注册项就宣称生产接入完成。
通用组件的测试工具只在 tests 临时注册，并不作为生产能力。

## 7. 输入协议与预算

模型输入从单独 max_top_k 改为 tool_limits[tool_name]；保留每个工具的独立限制。
提示词改为通用 TOOL_SELECTION_SYSTEM_PROMPT，知识用途由工具 description/schema 约束。
旧常量 KNOWLEDGE_QUERY_SYSTEM_PROMPT 为现有知识账本导入保留别名。
查询调用协议标识更新为 bound-tool-function-call/v2，工具声明与提示词继续进入摘要。
旧摘要运行不自动按新协议接续，保持冲突拒绝，不重写历史。

模型请求构造、初始规划和 Observation 使用通用 runtime.timeout_seconds；
实际知识工具仍使用工具预算与运行预算中的较小值。
旧 AttemptAttribution.execution_context 仍是知识绑定的审计快照，不能误读为
已实现覆盖所有通用阶段预算的新版运行审计契约；将来通用账本版本需一并演进。

## 8. 验证场景

新增 test_generic_task_runtime.py：

1. 没有任何 KnowledgeSearchTool 绑定，临时安装两个测试读取工具，模型选择 B，只有 B handler 执行。
2. 未注册绑定、能力不匹配绑定不会提供给模型。
3. 模型伪造选择已注册但未绑定的 knowledge_search，在 handler 前失败。
4. 无绑定时不调用模型，返回 TOOL_NOT_BOUND。
5. Observation 业务组件不提前提取知识执行上下文。
6. 当前旧账本拒绝其他工具，在 produce 前停止。

新增真实 PostgreSQL 测试：load_runtime_context 和 recovery._authorize 可以在 tools=() 时
核验案件与 READ/CONTINUE，不隐式要求知识配置。既有 native tool/checkpointer 测试继续验证
知识请求、结果和恢复复用。本地只读 Observation 组件测试不表示生产可绕过知识历史读取权限：
持久化 phase/access 仍按当前知识契约检查历史数据范围。

关联说明：[工具注册](workflow-tool-registration.md)、[可信环境与工具绑定](workflow-trusted-tools.md)。


## 9. 本次实际验证

- 相关身份、请求、工具、Observation 测试：89 passed。
- 通用边界与图/Invocation 测试：23 passed。
- 启用真实 PostgreSQL 的后端全量回归：582 passed、7 skipped，1 条既有依赖弃用警告。
- 最终补充非法工具名类型保护后，针对性复核：43 passed（包含新增 3 个非法类型用例）。
- git diff --check 通过。模型与检索使用替身，没有付费 API 调用。

测试读取和写入仅在 UUID 隔离 schema 中完成，未升级业务数据库 schema。


## 10. 知识客户端依赖下沉

知识专用依赖现在保存在 KnowledgeSearchTool 的 keyword-only 字段：

```python
knowledge = KnowledgeSearchTool(
    execution=knowledge_context,
    authorize=knowledge_policy,
    session_factory=sessions,
    embedding_client=embedder,
    milvus_client=milvus,
)
```

- 通用 graph 构建和 InvestigationRunner 不再接收 embedding_client / milvus_client。
- nodes.task_execution 和 execute_registered_tool 不再接收这些客户端或知识用 session_factory。
- graph/runner 保留 session_factory 用于账本、规划和恢复，不能与工具的读库依赖混为一谈。
- _knowledge_handler 选中知识工具后读取绑定字段，传给现有 7A 执行入口。
- 应用应在绑定中明确提供正确的知识 authoritative store 会话工厂，通常复用账本使用的 sessions；
  不能依赖 runner 再隐式转交其会话工厂。
- None 保留既有知识适配器的默认客户端/数据库解析语义，不新增全局容器或客户端工厂。
- 新字段仅保存应用已经创建的引用；客户端的创建、共享策略和关闭由应用负责。
- 字段不参与 repr/equality，不进入模型声明、scope、稳定业务摘要或 checkpoint；
  这不是把客户端变成不可变对象，只有绑定字段赋值受 frozen 约束。
- API 移除了旧客户端透传参数，不保留两条配置来源；旧调用方改为构造工具绑定。
- 未修改 schema、工具参数契约、提示词或查询协议摘要。

本次依赖下沉回归：591 passed、7 skipped，1 条既有依赖警告；包括真实 PostgreSQL、客户端绑定传递、检查点排除依赖对象与恢复不重复检索。git diff --check 通过。

执行模块的最新职责划分见 [执行模块指南](workflow-execution-module-guide.md)。原 `recovery.py` 已退为旧导入兼容，正式实现位于 `runner.py` 等对应模块。
