# 最小工具注册与原生模型工具选择

## 1. 本次范围

参考 DevF 的“名称映射到工具声明和执行函数”，使用已有 OpenAI-compatible SDK 的原生 function calling。
不引入 LangChain StructuredTool 新依赖、Registry 类、Factory、新图节点、自由 ReAct 循环或第二次模型调用。
当前只有 knowledge_search，不能宣称多工具选择质量或其他 capability 已实现。

## 2. 三个不同职责

| 对象 | 所在文件 | 负责什么 |
|---|---|---|
| ToolDefinition / TOOL_DEFINITIONS | execution/tools.py | 静态名称、说明、capability、参数契约、handler 白名单 |
| KnowledgeSearchTool | workflow/knowledge_tool.py | 按次授予的知识范围、身份和独立授权绑定 |
| KnowledgeSearchToolRequest | execution/contracts.py | 模型能提出的 query、filters、top_k |

注册不授予权限。模型不提供 actor、execution_id、scope 策略、profile、collection 或 handler。
KnowledgeSearchTool 仍是之前的可信绑定类，本次为减少迁移成本不改其类名。

TOOL_DEFINITIONS 是只读 MappingProxyType，不能由模型或 State 动态增加函数。
每次声明都从专用请求模型生成新的 schema，外部修改返回字典不会修改注册定义。

## 3. 实际调用链

```text
正式知识 Task
    ↓
按 capability 获取当前注册的声明（当前绑定已在可信运行边界检查）
    ↓
Agent.formulate_knowledge_query 一次模型请求
    tools=[knowledge_search 的名称、说明、参数 schema]
    tool_choice=required
    parallel_tool_calls=false
    ↓
模型返回原生 tool_calls：选择工具 + 查询参数
    ↓
parse_tool_choice
    完整结束标志 / 恰好一个 function call / 白名单名称 / capability / 专用参数校验
    ↓
KnowledgeSearchToolRequest
    ↓
QUERY_FORMULATION 账本提交
    ↓
execute_registered_tool(knowledge_search, ...)
    ↓
已注册 handler → 既有 execute_knowledge_search
    ↓
scope 收紧 + 知识权限 + Dense Retrieval + Context Resolution
    ↓
TaskExecutionResult → 原 KNOWLEDGE_EXECUTION 账本
```

不再通过普通正文 JSON 选择查询；文本回答即使包含合法 query JSON 也拒绝。
无调用、多调用、未知工具、自创类型、截断、伪造可信字段、超出 max_top_k 均不会进入检索。
查询失败仍沿用 QUERY_FORMULATION_INVALID 等既有失败结果语义；取消继续向上传播。

当前工具是 required，并非指定 function.name 强制调用。模型收到可用工具声明后返回工具名，
代码验证选择；因为当前只有一个合法工具，选择空间只有一个。
不开放无限循环或让模型自行执行。执行结果仍由独立 Observation 阶段解释，
不新增一套 assistant/tool 消息回合并绕过正式 Task。

## 4. 参数与执行边界

- 工具 schema 只包含 query、filters、top_k；additionalProperties=false。
- 暂不依赖供应商 strict schema 模式，声明 strict=false；实际 Pydantic 仍 extra=forbid。
- required 和 parallel_tool_calls=false 只是请求约束；客户端仍检查恰好一次调用，不能盲目信任供应商。
- Agent 只传任务意图、原始规划背景、goal、expected_result、max_top_k，不传可信身份/执行 ID。
- 工具 handler 不通过 eval、动态 import 或反射执行模型提供的名称。
- dispatch 失败返回无部分 payload 的 REJECTED；真正工具授权拒绝沿用 7A。
- 原 7A 本身的参数校验、scope 限制、timeout、来源校验和错误净化不重写。
- 结果的 task_id/capability 仍必须在提交前经 nodes._task_result 验证。

当前唯一选择在校验后投影为既有 KnowledgeSearchToolRequest。由于账本契约已固定
`tool_name: Literal["knowledge_search"]`，并没有丢失当前可执行工具身份。
增加第二个工具之前必须扩展查询产物与版本化持久化契约；不能只新增映射然后仍固定执行 knowledge_search。

## 5. 摘要与历史兼容边界

数据库 schema、BusinessOperationInput/PhaseArtifact 的字段与旧记录均不改写。
query_contract_digest 将以下内容一起纳入 SHA-256：

- 工具选择协议版本 knowledge-function-call/v1；
- system prompt；
- 工具名称、说明、完整请求 schema；
- tool_choice 与 parallel_tool_calls 设置。

现有 FirstRoundSpec.query_prompt_digest 字段复用为完整查询调用契约摘要，
随后 QUERY_FORMULATION 的 prompt_version 继续来自该稳定规格。

重要：本次从普通 JSON 查询升级为 function calling，会改变摘要。
已有绑定旧摘要的 run/规划根记录不能自动按新协议继续；仓储会明确冲突，
不会覆盖旧规格、默默迁移或重复调用。部署前必须清点在途案件，
需要继续的旧运行应使用与其规格匹配的旧版本执行环境；当前未新增跨版本运行迁移能力。
这沿用 fail-closed 的规格一致性边界，不能把“不改 schema”误读成“旧运行自动兼容”。

## 6. 测试

- agents/test_query_formulation.py：原生调用、无 objective fallback、可信字段伪造、
  未知工具/多调用/文本回退/截断/错误类型/数量越界。
- workflow/test_registered_tools.py：声明隔离、只读映射、能力不匹配、未知名称、
  原适配器分派、独立授权拒绝、工具说明参与摘要。
- workflow/test_native_tool_postgres.py：真实 PostgreSQL/checkpointer；使用真实 Agent 的
  原生调用解析方法和受控 SDK 响应替身，验证成功与非法选择的持久化结果及恢复复用，
  并验证调用契约变化不会静默接续已有 run。
- 既有任务身份、Observation、首次规划、崩溃恢复、运行锁与 generation 测试继续保留。

测试不调用付费模型。它证明工程链路与机械校验，不证明实际供应商/model 已支持相同参数，
也不证明 Query 或多工具选择的语义质量。不支持 function calling 的供应商会明确失败，不自动退回正文解析。

## 7. 参考

[OpenAI 官方 Function calling](https://developers.openai.com/api/docs/guides/function-calling)：
工具声明、required 和关闭 parallel_tool_calls 的接口约束。本项目使用现有 Chat Completions 客户端，
未切换模型或供应商。DevF 参考文件：api/routes/chat.py 的工具字典，以及
services/agents/chat_agent.py 的声明包装与已注册名称分派。


本次验证：focused tests 74 passed；启用真实 PostgreSQL 的完整 backend 回归
576 passed、7 skipped，1 条既有 LangChainPendingDeprecationWarning。
本次变更范围的 git diff --check 通过；模型与检索均为测试替身，无付费 API 调用。
