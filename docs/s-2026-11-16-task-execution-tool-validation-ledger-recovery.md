# 第四阶段：从正式任务到可靠保存的工具结果

日期：2026-11-16  
关键词：task_execution、工具注册、原生工具调用、阶段入口、事务、恢复、State

本文按当前实现重新梳理执行链路，集中解释阅读代码时提出的问题。示例中的查询和编号仅用于说明。本文只新增说明文档，不修改业务代码。

## 1. 先看整体：每个任务先形成请求，再执行工具

上一阶段已经把候选计划正式接受到数据库。执行阶段不会重新规划，而是逐个处理正式任务。

```text
图中的 execute_node
    ↓ 确定当前轮次，注入两个明确入口
task_execution
    ↓
对每个任务：
    query_phase
        ├─ 有可靠历史请求 → 复用
        └─ 没有 → 模型选择工具、填写参数 → 校验 → 提交
    ↓
    请求失败：收集失败结果，不执行工具
    请求成功：按当前上下文重新校验
    ↓
    tool_phase
        ├─ 有可靠历史结果 → 复用
        └─ 没有 → 执行注册工具 → 校验 → 提交
    ↓
    收集结果和任务状态
    ↓
全部任务处理完，返回 State 更新
    ↓
observation 节点解释结果
```

当前任务循环按计划顺序执行，并非并行 Agent 调度。async def 允许等待时让出事件循环，不会自动让多个任务并行。

## 2. 与旧版说明相比，哪些地方已经变化

旧的 ledger_phase_executor 和 execute_phase(kind, ...) 已删除，不应再按旧调用链阅读。

现在有三个职责明确的持久化入口：

| 入口 | 输入工作 | 返回给业务节点 |
| --- | --- | --- |
| query_phase | 生成工具请求的函数 | QueryOutcome：请求或失败结果 |
| tool_phase | 执行具体工具的函数 | TaskExecutionResult |
| observation_phase | 解释本轮结果的函数 | Observation |

三个入口都调用现有 _phase，共用事务和恢复机制；没有增加三个图节点，也没有第二套编排器。

主图仍是：prepare_plan → task_execution → observation；允许继续时回到 prepare_plan，否则结束本次图调用。

## 3. 代码跳转顺序

| 代码 | 阅读重点 |
| --- | --- |
| [execute_node](../backend/src/risk_platform/workflow/graph.py#L52) | 当前轮次、依赖注入、检查点格式转换 |
| [task_execution](../backend/src/risk_platform/workflow/nodes.py#L117) | 唯一任务循环 |
| [query_phase](../backend/src/risk_platform/workflow/phase_execution.py#L148) | 请求阶段输入、记忆和请求持久化 |
| [formulate_task_query](../backend/src/risk_platform/workflow/nodes.py#L56) | 可用工具、限制、超时和错误分类 |
| [formulate_tool_call](../backend/src/risk_platform/agents/investigation.py#L77) | 真正请求模型 |
| [available_tools](../backend/src/risk_platform/execution/tools.py#L155) | 注册、能力、绑定的交集 |
| [parse_tool_choice](../backend/src/risk_platform/execution/tools.py#L180) | 解析一次原生工具调用 |
| [validate_tool_call](../backend/src/risk_platform/execution/tools.py#L167) | 当前上下文下的调用校验 |
| [tool_phase](../backend/src/risk_platform/workflow/phase_execution.py#L180) | 工具阶段输入和可靠结果返回 |
| [execute_registered_tool](../backend/src/risk_platform/execution/tools.py#L203) | 白名单分派和结果检查 |
| [_knowledge_handler](../backend/src/risk_platform/execution/tools.py#L22) | 知识检索的专用实现入口 |
| [_phase](../backend/src/risk_platform/workflow/phase_execution.py#L56) | 三段短事务、结果复用和恢复 |
| [_selection](../backend/src/risk_platform/workflow/phase_execution.py#L26) | 固定本轮采用哪份结果 |

## 4. “图注入三个入口”是什么意思

注入就是把函数作为参数交给节点。任务节点只需要请求生成和工具执行两个入口，Observation 节点接收第三个。

图用 partial 提前填好依赖，例如：

```python
query_phase=partial(
    phase_execution.query_phase,
    sessions=session_factory,
    call=call,
    model_name=investigation_agent.model,
)
```

partial 不立即执行函数。节点随后调用 query_phase(query, task_id=..., capability=...) 时，预填的数据库、调用上下文和模型名才一起参与执行。

call 是现有 InvestigationInvocation，内部的 runtime 和 execution 仍保持职责区别，不新增上下文包装。只有确实需要身份、权限、账本和运行权的入口接收它；模型只接收筛选后的业务材料。

## 5. task_execution 先检查什么

入口读取 State 中的 InvestigationPlan 和 InitialPlanningInput，并重新按契约校验。恢复出来的对象可能是 JSON 字典，不能假定一直是 Python 模型实例。

然后检查：

- 两个阶段入口必须可调用，生产没有默认直通执行器。
- check_runtime 核对当前上下文、权限、绑定及调查输入一致性。
- task_id 非空且不重复。
- 任务非空且全部为 PENDING。

PENDING 要求针对本轮正式计划，不表示“只能执行第一轮”。任务节点返回的状态视图与数据库中不可变 AcceptedPlan 不同，恢复时通过账本复用已完成阶段。

结构校验本身不证明计划已经正式接受；正式来源由上游 prepare_plan、运行身份以及账本关联核验共同保证。

## 6. 工具“注册”不是写数据库

当前注册表是 Python 中的只读映射：

```python
TOOL_DEFINITIONS = MappingProxyType({
    KNOWLEDGE_SEARCH.name: KNOWLEDGE_SEARCH,
})
```

键是工具名，值是 ToolDefinition，包括参数模型、handler、执行限制和结果校验等。MappingProxyType 禁止直接增删替换映射条目，但不保证其中对象递归不可变。

注册不执行 handler。数据库阶段记录保存“发生了什么调用”，不承担当前静态注册表的职责。

```text
已注册：系统实现了这个工具
本次绑定：这次运行提供这个工具
授权通过：当前调用满足相应权限和资源范围
实际执行：handler 已被调用
可靠提交：结果已保存到账本
```

这些状态不能互相替代。当前生产注册表只有 knowledge_search；测试中的其他工具不代表已经上线同等生产能力。

## 7. query_phase：先找历史，再决定是否调用模型

query_phase 构造本任务的 ToolOperationInput，记录案件、轮次、计划、任务、阶段、能力、模型、协议摘要及工具限制。

接着定义 generate，把它交给 _phase。如果有可复用结果，generate 不会运行，也就不会重新读取生成所需记忆或调用模型。

真正生成时，generate 在短事务中检查权限和 Guard，取得本阶段记忆，然后调用节点提供的 query 函数。

query 函数调用 formulate_task_query：

```text
筛选工具 → 取得声明和限制 → 模型调用 → 校验具体调用
```

成功产物保存为带工具名和参数的 RegisteredCall；失败则保存结构化执行失败结果。提交后，再转换为节点使用的 QueryOutcome。

## 8. formulate_task_query 如何筛选工具

available_tools 取三者交集：已注册工具、任务要求的能力、本次可信工具绑定。

例如系统有 knowledge_search 和测试计算工具，任务能力是知识检索，本次也绑定 knowledge_search，那么只把匹配的工具声明交给模型。

这里的“可用”不表示服务网络正常、账号有余额，也不表示任意参数都可执行。它是候选范围筛选，不是完整授权或健康探测。

每个候选工具提供公开参数 schema 和 execution_limits。工具客户端、权限回调、数据库会话不进入模型输入。

## 9. 模型实际看到什么

formulate_tool_call 组织 payload：

```python
payload = {
    "investigation_context": ...,  # 用户问题或告警背景
    "goal": goal,                 # 整轮计划目标
    "objective": f"{task.objective}\n需要获取的信息：{task.expected_result}",
    "tool_limits": tool_limits,
}
```

历史记忆存在时再加入 historical_memory。这里已经把模型输入中的 objective 和 expected_result 合为一项，但持久化任务契约仍保留两个字段，避免直接破坏历史摘要和读取。

goal 是整轮目标，objective 是当前任务；两者有区分价值，但填写成重复文字时仍会造成冗余。需要获取的信息不能预设“证明某原因导致异常”。

实际模型入口是 self.client.chat.completions.create。tools 提供工具声明，调用选项要求原生工具选择且关闭并行调用。它只生成工具请求，不执行工具。

response.choices[0] 取第一个候选回答，不是第一个工具调用。当前实现按单候选使用，尚未显式验证 choices 恰好为一项。

## 10. to_thread、await 和超时

同步模型调用放在 asyncio.to_thread 中，避免等待网络时阻塞事件循环。当前协程 await 结果，其他协程可以继续运行；不是当前调查无需等待。

它不是持久化后台任务。进程退出不会自动恢复线程，外层超时也不保证线程和远程请求立即停止。线程共享内存，因此输入做深复制，不能把线程当作数据隔离设施。

## 11. 两种调用校验有什么区别

parse_tool_choice 检查模型响应：完整 tool_calls 结束、恰好一次 function 调用、工具在注册及本次提供范围中、参数 JSON 符合请求模型。不接受普通正文代替调用，也不退回 task.objective 当查询词。

validate_tool_call 检查当前执行上下文：

1. check_runtime 检查继续调查的权限及上下文。
2. 重新筛选当前可用工具，核对具体名称。
3. request_model 检查参数结构。
4. validate_request 检查工具专有的本次限制。

格式正确不表示范围合法。例如结构允许 top_k 为整数，但本次上限为 5，传入 20 仍应拒绝。

validate_tool_call 返回校验后的 ToolCall，不执行工具、不提交数据库。任务循环在 query_phase 返回后再调用它：恢复可能直接复用请求、跳过模型和 formulate_task_query，因此执行前不能只依赖过去生成时的检查。

## 12. 参数生成失败是否继续执行工具

不会。query 返回失败 QueryOutcome 时，任务循环直接收集失败结果。

| 情况 | 典型处理 |
| --- | --- |
| 能力不支持、工具未绑定 | REJECTED |
| 模型输出或参数非法 | FAILED |
| 模型超时、连接失败 | FAILED，并保留可重试属性 |
| 未分类异常 | 当前兜底 INTERNAL |

retryable 只是属性，不代表这里立即重试。当前 formulate_task_query 没有单独分类 InvestigationAccessError，部分权限异常可能进入 INTERNAL 兜底，这是现存待改善点，不能把它解释成已经正确区分所有错误。

## 13. tool_phase 和 execute_registered_tool

请求通过校验后，task_execution 定义 execute_selected，并交给 tool_phase。

tool_phase 记录真实工具名、能力、参数和限制，调用 _phase 执行或复用。实际执行时才进入 execute_registered_tool：

```text
工具是否注册、能力是否匹配？
    ↓
check_runtime、具体工具是否本次绑定？
    ↓
请求模型校验、工具专有参数校验
    ↓
definition.handler(task, request, runtime_context=...)
    ↓
检查结果契约、task_id、capability 和工具名
    ↓
补齐实际工具名和结果版本
    ↓
返回 TaskExecutionResult
```

工具不存在、能力不符、未绑定的明确分支返回 REJECTED；其他权限、校验或实现异常可能直接抛出，并非所有异常都转换为结果。

当前知识检索进入 _knowledge_handler，取得专用知识范围、独立授权及客户端，再调用 execute_knowledge_search。非知识工具结果在通用入口按 result_model 和 validate_result 检查；知识工具仍由专用路径承担相应校验，不能称为完全没有特例。

工具结果 task_id 或能力不匹配时，必须在入账前拒绝；不能先保存再到更新 State 时才检查。

## 14. 每次保存前检查的是谁

不是每一行 session.add 都重新鉴权，而是在事务边界检查，事务内完成相关写入。

| 检查 | 保护什么 |
| --- | --- |
| check_runtime | 业务身份、案件轮次归属、资源权限回调、绑定和可选输入一致性 |
| authorize_run | 数据库正式归属及 READ、CONTINUE 权限 |
| Guard.check_lock | 本进程持锁连接仍有效 |
| Guard.check | 运行锁和 generation，防止旧执行者提交 |
| 请求、结果关联校验 | 参数范围正确，结果属于当前正式任务 |

业务身份回答“actor-A 能否调查 case-1”；执行身份回答“run-1 的当前代次是否仍有权推进”。模型和 State 不能自行授予这两种权力。

同一输入在直接调用链中反复检查可能冗余，当前仍存在部分重复；跨外部等待、事务、恢复和具体工具边界的检查有不同作用，不能一概删除。权限撤销能否及时生效也取决于实际授权回调是否查询最新策略。

## 15. _phase 的三个短事务

```text
事务一：鉴权、运行权、查历史、固定可复用结果
    ↓ 没有可用结果才继续
事务二：检查重试策略，登记 STARTED 和运行代次
    ↓ 提交
事务外：produce() 调用模型或工具
    ↓
事务三：重新鉴权和检查运行权
        保存结果、COMMITTED、本轮采用关系
    ↓ 提交
返回产物
```

等待外部服务时不持有前面短事务的行锁，但外层 Runner 的会话级运行锁持续持有。flush 不等于最终提交。

Observation 使用同一个 _phase；其确定性记忆提取与结果同事务提交，避免 Observation 已保存但记忆丢失的额外窗口。

COMMITTED 表示产物可靠保存，不保证工具业务成功。产物可以明确包含 FAILED 或 REJECTED。

## 16. 什么时候复用，具体比什么

典型窗口：模型请求已经提交，工具未执行就中断；恢复读取原请求，跳过模型。工具结果已提交但节点检查点未更新时，则请求与结果都可以分别复用。

操作编号由五项身份生成：investigation_id、round_id、plan_id、task_id、operation_type。不是比较自然语言相似度，新一轮不会因文字类似而自动复用上一轮。

请求生成输入还记录 capability、model_name、prompt_version、tool_limits 和 schema_version。未正式选定结果时，操作登记比较完整输入，变化则冲突。

已经为本轮选定结果时，_phase 比较输入会排除 model_name 和 prompt_version，允许读取原模型的历史产物；这不是声称用新模型重做。其他已记录业务字段仍必须一致。

_selection 再检查：结果关联同一操作、对应已提交产物、已固定选择不能更换；没有固定选择时，必须唯一或由策略明确指定，不自动取最新成功。

这里没有逐项比较完整模型请求正文。任务目标依赖不可变正式计划及上游核验；完整记忆、goal、objective 并未直接存入该阶段输入，因此不能宣称“完整 Prompt 复现”。

仅有 STARTED 没有可靠产物不能复用。旧代次遗留尝试经归属核实后结算 UNCERTAIN，再按显式策略决定是否重试；新尝试不覆盖旧记录。

旧 v2 阶段不再自动转换为 v3 续跑。计划存在旧阶段记录时明确阻止执行，历史读取器仍可用于核查；本文不代表已经迁移旧数据。

## 17. 结果保存和 State 更新的先后

```text
task-1 请求提交 → 工具结果提交 → 加入本地 results
task-2 请求提交 → 工具结果提交 → 加入本地 results
    ↓
节点返回 investigation_plan、task_execution_results
    ↓
LangGraph 更新 State、保存检查点
```

每个阶段各自提交，不是所有工具一起提交一个大事务。task_execution 返回的是任务状态副本，不修改数据库中已接受计划。

SUCCEEDED 对应任务 COMPLETED，其他执行状态映射为任务 FAILED；REJECTED 与 FAILED 的详细区别仍保留在执行结果中。工具零命中也可能执行成功，不能把 COMPLETED 等同于已经回答调查问题。

返回字段替换 State 的对应字段，未返回字段保留；上一轮不可变记录留在数据库。节点结果列表不是追加型 reducer。

## 18. 故障场景与实际边界

| 故障点 | 行为 |
| --- | --- |
| 请求生成得到已知业务错误 | 保存失败事实，不调用工具，通常继续后续任务 |
| 工具返回结构化失败 | 保存失败结果，交给后续解释 |
| 结果身份错误、账本冲突、锁失效 | 抛异常，中止节点，不返回部分 State 更新 |
| 工具已返回，结果提交前退出 | 可能遗留 STARTED，不能认定外部未执行 |
| 结果已提交，检查点未更新 | 恢复从账本复用，不盲目重复调用 |
| 外部等待期间失去权限或代次 | 提交前拒绝，不能接受过期执行者产物 |

不重复领取不等于保证完成，持久化不等于工具绝不重复。将来接入有外部副作用的工具仍需稳定幂等键及专用恢复核实。

此链路已做相关单元及隔离 PostgreSQL 测试；此前代码改动报告为 270 项单元测试通过、119 项跳过，28 项选定 PostgreSQL 集成测试通过。该结果不等于全仓库、真实模型或生产环境验收；本次文档编写仅核对代码、链接和示例。

前文可接着阅读 [规划、正式接受与恢复](s-2026-11-16-planning-candidate-acceptance-state-recovery.md)。后续 Observation 的发现、未解决问题和推进决定属于解释阶段，不由 task_execution 自由决定。
