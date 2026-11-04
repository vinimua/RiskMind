# 通用可信执行环境与知识查询工具

## 1. 本次改变解决什么问题

原来的 InvestigationRuntimeContext 直接保存 KnowledgeExecutionContext、authorize_knowledge
和 allow_unscoped，把整个调查运行环境等同于知识查询环境。现在分为案件运行环境和工具专用绑定。

```text
InvestigationInvocation
├── runtime: InvestigationRuntimeContext
│   ├── investigation / round：canonical 案件与轮次
│   ├── actor_id：已认证身份
│   ├── authorize_resource：READ / CONTINUE 资源权限
│   ├── timeout_seconds：当前运行的阶段超时预算
│   └── tools：可信应用明确安装的工具绑定元组
│       └── KnowledgeSearchTool
│           ├── execution：知识专用范围、profile、top_k、超时
│           ├── authorize：知识工具独立权限
│           └── allow_unscoped：无案件模型时显式许可知识范围
└── execution：内部运行锁、generation、规格和恢复策略
```

通用 runtime.py 不导入知识模块或 KnowledgeExecutionContext；知识特有逻辑集中在 knowledge_tool.py。
Invocation 与 execution 控制信息不进入业务 State 或 checkpoint。

## 2. 可信不等于只做类型声明

check_runtime 按顺序检查：

1. 上下文类型、非空 actor、有限且大于零的运行超时。
2. canonical Investigation/Round 的结构与案件归属。
3. 可选的本次 planning_input 与案件原始输入完全一致。
4. authorize_resource 对 READ 或 CONTINUE 必须显式返回 True；异常被净化。
5. tools 必须为元组，工具名非空且不重复，绑定必须符合 TrustedToolBinding 协议。
6. 每个绑定 validate_binding(actor_id, resource)，核对工具与当前身份、资源的绑定。

TrustedToolBinding 是小 Protocol，只定义 name 与 validate_binding；没有 Factory、动态导入或
通用调度框架。它只表达可信工具绑定的最低约束，不宣称协议符合就能执行工具。
当前节点仍明确执行知识任务，其他 capability 仍不可执行。

## 3. 知识查询是第一个实际工具

KnowledgeSearchTool 是现有 7A 知识工具的可信配置适配器，不重新实现检索算法。

- execution.actor_id 必须等于通用环境 actor_id。
- 案件有 model_id 时，execution.scope.model_id 必须完全一致，不能缺失。
- 无案件 model_id 时，必须显式设置该绑定 allow_unscoped=True。
- 必须提供独立的 authorize 函数，资源 CONTINUE 权限不能替代 knowledge:search 权限。
- knowledge_execution() 明确寻找具体 KnowledgeSearchTool 类型；缺少绑定或同名伪造类型直接拒绝。
- 有效工具超时取工具超时与运行预算的较小值，不修改原始绑定。
- knowledge_authorizer 每次重新检查资源 CONTINUE 和知识工具权限；异常不泄露且按拒绝处理。

```text
正式任务
→ Query Formulation
→ KnowledgeSearchToolRequest 严格参数契约
→ 知识绑定与资源权限复核
→ execute_knowledge_search（既有 7A）
→ Dense Retrieval
→ Context Resolution
→ TaskExecutionResult
```

工具请求不携带 actor/profile/collection；这些仍来自可信绑定。模型不能通过 State 安装工具。

## 4. 应用如何构造

```python
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.workflow.access import load_runtime_context
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.workflow.runtime import InvestigationAction
from risk_platform.workflow.invocation import InvestigationInvocation

# actor_id 必须由应用认证确定，下面的策略函数必须由应用实现。
knowledge = KnowledgeSearchTool(
    execution=KnowledgeExecutionContext(
        actor_id=actor_id,
        index_profile="baseline-v1",
        scope={"model_id": case_model_id},
        max_top_k=5,
        timeout_seconds=20,
    ),
    authorize=knowledge_policy,
)
async with sessions() as session:
    context = await load_runtime_context(
        session,
        investigation_id=investigation_id,
        round_id=round_id,
        actor_id=actor_id,
        authorize_resource=resource_policy,
        tools=(knowledge,),
        timeout_seconds=30,
        action=InvestigationAction.CONTINUE,
    )
result = await runner.ainvoke(context=InvestigationInvocation(runtime=context))
```

timeout_seconds 是阶段等待预算，不是整个图的总时长 deadline；当前仅支持知识调查的 runner 各阶段继续使用
不超过运行预算的知识上下文预算，以保持既有 Attempt 超时归因和恢复契约一致。
通用环境允许 tools=()，但当前知识调查 runner 会在外部调用和创建 Attempt 前拒绝缺少知识绑定。

## 5. 持久化与恢复兼容

本次修改 Python 构造接口，不改数据库 schema，不改历史输入版本或摘要。

- FirstRoundSpec 仍表达当前已实现的知识调查执行规格。
- 既有 BusinessOperationInput 与 AttemptAttribution 的知识 payload 保持原契约。
- access.py 在当前知识账本操作边界取得知识上下文，继续验证历史范围。
- READ 与 CONTINUE 保持分权；恢复时重新从 PG 验证案件事实。
- 同一 topology、阶段业务循环和三事务 phase executor 保持不变。
- 不保留旧 execution=/authorize_knowledge= 的 Runtime 构造别名，避免形成双重身份来源。

后续接入事实查询时，须新增专用请求契约、可信绑定、执行适配、授权、账本产物及测试；
不能仅增加 capability 枚举值就宣称支持。FirstRoundSpec 等知识专用持久化契约届时需要版本演进，
本次不提前设计一个 Any payload 替代它们。

## 6. 记忆边界

这不是记忆模块。State 仍保存本轮计划、结果、Observation；账本记录持久历史。
以后给 Agent 提供跨轮记忆视图，应由授权后的历史读取与可追溯摘要完成，不把原始权限、
连接、回调函数放进记忆或模型提示。当前没有新增跨轮记忆、Replanning 或其他工具能力。

## 7. 验证要点

新增 test_tool_runtime.py 覆盖无工具的通用环境、其他测试绑定、缺少知识授权绑定、非法运行预算、
actor 不匹配、重复/字典伪造绑定、保留工具名伪造、预算收紧和权限撤销。
既有运行身份隔离、案件范围、READ/CONTINUE、知识拒绝、真实 PostgreSQL 账本和恢复测试继续运行。


本次完整后端回归（启用真实 PostgreSQL 集成）：558 passed、7 skipped，1 条既有
LangChainPendingDeprecationWarning。git diff --check 通过。
测试仅操作隔离临时 schema，没有对业务数据库执行迁移，也没有调用付费模型 API。
