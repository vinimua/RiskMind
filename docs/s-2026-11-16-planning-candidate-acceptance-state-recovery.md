# 从调查问题到正式计划：规划链路、事务与恢复

日期：2026-11-16  
关键词：prepare_plan、initial_plan、候选计划、正式接受、State、运行身份、事务、恢复

本文按当前代码解释规划链路，并集中回答阅读过程中提出的问题。示例中的任务和编号仅用于说明，不代表真实工具执行结果。本文只记录代码，不修改业务实现。

## 1. 先抓住职责：模型建议，程序接受，节点执行

假设用户要求：“调查 credit-v3 最近的异常。”模型提出两个任务：检索规则变更记录、检索生效版本说明。

模型返回任务不代表系统已经批准执行。完整流程是：

```text
plan_node：确认当前轮次
    ↓
prepare_plan：检查正式计划是否已经建立
    ├─ 已建立 → 从数据库读取
    └─ 尚未建立
          ↓
       _planning_candidate：取得可靠保存的候选
          ↓
       确定性校验、正式接受、建立执行代次
          ↓
       从数据库读取正式计划
    ↓
返回字段更新，LangGraph 更新 State
    ↓
task_execution 执行正式任务
```

| 对象或记录 | 表达的事实 | 能否据此执行任务 |
| --- | --- | --- |
| 模型返回的 JSON | 模型提出了建议 | 不能 |
| 内存中的 InvestigationPlan | 建议已解析为任务对象 | 不能 |
| 已提交的 PlanningCandidate | 候选已经可靠保存 | 不能 |
| 正式 AcceptedPlan 及接受关联 | 程序已经接受本轮计划 | 还需运行权、权限、工具检查 |

“正式接受”是程序业务动作，不等于用户在聊天界面确认启动。用户确认启动发生在入口；这里接受的是具体任务计划。

## 2. 代码阅读地图

按下面顺序阅读，可以避免在辅助函数之间迷路：

| 代码 | 负责什么 |
| --- | --- |
| [plan_node](../backend/src/risk_platform/workflow/graph.py#L54) | 图节点接线，取得当前轮次上下文 |
| [current_round_call](../backend/src/risk_platform/workflow/planning.py#L185) | 沿正式后继关系核对当前轮次 |
| [prepare_plan](../backend/src/risk_platform/workflow/planning.py#L64) | 组织候选生成、正式接受与读取 |
| [round_material](../backend/src/risk_platform/workflow/planning.py#L239) | 从账本组装历史调查材料 |
| [planning_specification](../backend/src/risk_platform/workflow/planning.py#L37) | 记录候选生成依据 |
| [_planning_candidate](../backend/src/risk_platform/workflow/planning.py#L118) | 登记尝试、生成或复用候选 |
| [nodes.initial_planning](../backend/src/risk_platform/workflow/nodes.py#L29) | 整理参数，调用 Agent |
| [InvestigationAgent.initial_plan](../backend/src/risk_platform/agents/investigation.py#L120) | 真实模型请求和草案解析 |
| [planning_repository.validate](../backend/src/risk_platform/workflow/persistence/planning_repository.py#L48) | 校验候选并构造待接受对象 |
| [load_accepted_plan](../backend/src/risk_platform/workflow/planning.py#L54) | 从正式数据库记录构造 State 更新 |

行号随代码注释增加可能变化，以函数名为准。

## 3. plan_node：图节点不重复实现规划业务

核心代码如下：

```python
call = await planning.current_round_call(
    state, require_run_context(runtime), sessions=session_factory
)
return await planning.prepare_plan(
    state, call.execution,
    sessions=session_factory,
    agent=investigation_agent,
    runtime=call.runtime,
)
```

第一步拿到当前轮次的可信上下文；第二步把工作交给 prepare_plan。节点没有自行调用模型、写正式计划或提交事务。

这里三个名称容易混淆：

| 名称 | 含义 |
| --- | --- |
| LangGraph 的 runtime | 本次图调用的运行容器，context 中放 InvestigationInvocation |
| call.runtime | 可信业务上下文：身份、案件、轮次、工具绑定等 |
| call.execution | 内部执行控制：运行身份、Guard、恢复策略及预算配置 |

这些控制依赖不会作为普通业务 State 保存，也不会全部发给模型。

## 4. 为什么需要 current_round_call

同一次图调用可以连续推进多轮：

```text
root：run-1 / round-1 / plan-1
State：第一轮完成后，改为 run-2 / plan-2
```

root 是最初的可信调用起点；State 表达流程目前指向第二轮。第二轮不能继续以第一轮身份登记结果，也不能因为 State 写了 run-2 就直接相信它。

current_round_call 的步骤是：

1. 从 State 取得目标 run_id，没有时使用 root 身份。
2. 检查 root 对应权限和仍持有的运行锁。
3. 从起点沿数据库 RoundProgressionRow 逐轮查找目标。
4. 核对每条关系的案件、根运行、来源运行和后继轮次；检测循环。
5. 核对 State 的 plan_id 与正式后继关系一致。
6. 仍在起始轮次就直接返回 root。
7. 已切轮则读取正式轮次记录、检查权限及已有运行身份，再构造本轮上下文。

新上下文继续使用原来的锁连接，但使用当前轮次自己的 run_id、round_id、plan_id 和 generation。它不创建后继轮次，不重新获取另一把锁，也不执行计划。

generation 是执行代次，用于阻止旧执行者提交，不是调查轮数；尚未绑定正式运行时可以为 0。

## 5. prepare_plan：先看是否已经有正式执行代次

```python
if invocation.guard.generation == 0:
    # 取得候选并正式接受
    ...
return await load_accepted_plan(...)
```

generation 为 0，表示尚未绑定正式执行代次，需要走候选与接受流程，不表示“没有锁”或“没有权限”。Runner 已经持有运行锁。

已有正式执行代次时，跳过候选生成，直接读取正式计划。首次接受和恢复最后都从数据库读取，避免正式执行继续依赖未经核对的内存候选。

## 6. 规划输入来自哪里

### 6.1 原始问题和可用能力

InitialPlanningInput 包含用户查询或告警，两种来源必须恰好提供一种；同时列出本次提供的能力。案件的原始输入是权威来源，不能让模型或 State 自由替换。

### 6.2 历史 Observation 和任务

round_material 从正式推进记录找到历史轮次，再读取已选择的 Observation、AcceptedPlan 和执行结果，组装：

```text
previous_observations：此前发现和仍未解决的问题
previous_tasks：此前实际做过什么，状态和结果是什么
remaining_task_budget：总任务额度减去历史任务数量
```

这里没有 next_focus，不要求每轮只处理一个问题。模型可以根据全部材料选择下一轮问题子集，形成多任务计划。

必须区分：工具成功但零命中、工具失败、调用被拒绝。相同请求在数据快照变化后也可能值得重查，因此历史摘要保留实际请求，并在有对应载荷时保留快照。通用工具不一定具备知识检索的 snapshot 字段。

这些摘要不是另一套权威历史；权威事实仍在原计划、结果和 Observation 账本。Prompt 要求避免无增量重复，但不能据此宣称已经证明语义去重正确。

### 6.3 记忆

_planning_candidate 通过 context_for_stage 取得本阶段记忆，再交给模型。记忆提供过往背景；历史 Observation 提供本次调查正式提交的结果解释，两者不能混为一谈。

历史解释不是当前根因，知识和记忆正文也不能赋予执行权限。

### 6.4 planning_specification 不是完整 Prompt 快照

它记录运行身份、模型名、原始调查输入摘要，以及 Prompt 与历史材料的摘要。register 会比较已登记记录，输入不一致时拒绝覆盖。

摘要可以发现对应材料发生变化，但不能还原正文，也不能单独保证完全复现模型请求或输出。

## 7. _planning_candidate：三个事务包住一次模型请求

```text
事务一：鉴权、检查锁、读取记忆、登记输入、查历史
    ↓
事务二：检查重试策略、保存 STARTED
    ↓ 提交
事务外：调用模型，校验候选
    ↓
事务三：保存候选和 COMMITTED
```

### 7.1 事务一：有可靠候选就复用

register 固定生成依据，history 读取并锁定本次运行的规划尝试。

已有 COMMITTED 候选时，从数据库解析、检查摘要和业务规则后返回，保留原任务 ID，不再调用模型。

遗留 STARTED 若属于当前 claim，则拒绝重复调用；其他旧 claim 的遗留尝试按当前持锁恢复路径结算为 UNCERTAIN。它表达“没有可靠保存结果”，不能推断外部模型没有被调用。

### 7.2 事务二：先记账再发请求

代码再次鉴权、检查锁，按恢复策略判断是否允许重试 UNCERTAIN 或 FAILED。允许时新建尝试，保存 STARTED、发起者、claim 和序号。

事务提交之后才发出模型请求。flush 只是把变更发给数据库，正常退出 sessions.begin() 才完成提交。

### 7.3 外部请求不占用上述事务

```python
async with asyncio.timeout(trusted.timeout_seconds):
    update = await asyncio.to_thread(
        nodes.initial_planning,
        {"planning_input": source.model_copy(deep=True)},
        investigation_agent=agent,
        memory_context=memory_context,
        **(material or {}),
    )
```

深复制原始输入，减少同步工作修改上游对象的风险。to_thread 把同步模型链路放到线程，避免直接阻塞事件循环；不是可靠后台任务。

等待模型时没有一直开启前面的账本事务，但外层会话级运行锁仍保持。异步等待超时不保证线程或已发出的远程请求立即停止，不能据此认定没有费用或没有调用。

### 7.4 校验后保存候选

```python
candidate = PlanningCandidate(plan=update["investigation_plan"])
accepted = planning.validate(candidate, spec, source)
```

validate 重新检查契约、非空目标和任务说明、允许且已注册的能力，并构造待接受的 AcceptedPlan。随后检查任务数量预算。

accepted 只是变量名：对象类型叫 AcceptedPlan，不表示数据库已经正式接受。

合法候选在事务三与 COMMITTED 一起保存。此处的 COMMITTED 只表示候选可靠落库，仍不能执行任务。

## 8. nodes.initial_planning：辅助函数，不是第四个图节点

这个函数组装可选参数，调用 Agent.initial_plan，然后返回计划及本轮状态更新。虽然放在 nodes.py，实际主图仍是 prepare_plan、task_execution、observation 三个节点。

在候选路径中，上层主要取其 investigation_plan；这份内存返回值没有直接作为正式计划放行。接受完成后，还要通过 load_accepted_plan 从正式记录读取。

## 9. initial_plan：模型调用和输出解析

### 9.1 输入参数

| 参数 | 用途 |
| --- | --- |
| planning_input | 原始调查问题或告警、可用能力 |
| memory_context | 已选择的历史记忆 |
| previous_observations | 此前已提交的发现及未解决问题 |
| previous_tasks | 真实执行过的任务和结果摘要 |
| remaining_task_budget | 本轮不能超过的剩余任务数量 |

名字叫 initial_plan，但后续轮次也复用它，不能理解为只执行第一轮。

### 9.2 发请求前

从 TOOL_DEFINITIONS 收集注册能力，与本次能力取交集。没有交集直接拒绝，不调用模型。组装调查 JSON，并仅提供已注册的能力。

有历史 Observation 时重新检查其结构、ID 不重复，并要求剩余任务预算为正整数；这些结构检查不证明来源真实，来源由上游账本读取链路保证。

### 9.3 真正的大模型入口

```python
response = self.client.chat.completions.create(
    model=self.model,
    messages=[
        {"role": "system", "content": INITIAL_PLANNING_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
    ],
    response_format={"type": "json_object"},
)
```

system 放规划规则，user 放调查材料。上下文不是另一个神秘通道，就是这份实际发送的 JSON。此请求没有 tools 参数，只生成任务，不执行工具。

json_object 要求 JSON 对象，不保证业务字段正确，必须继续解析和校验。

### 9.4 输出处理

1. finish_reason 必须为 stop，拒绝截断或中止的生成。
2. 正文必须存在。
3. _InitialPlanDraft 解析 JSON；禁止额外字段，任务至少一项。
4. 检查任务数和本次提供的能力。
5. 转为 InvestigationPlan，由程序补任务 ID 和 PENDING 状态。

Agent 内能力输出检查使用原 available_capabilities；后续 planning.validate 才进一步使用“本次允许 ∩ 已注册”的集合。不能只看 Agent 一层就称为完整能力校验。

## 10. expected_result 为什么存在

```python
InvestigationTask(
    objective=task.objective,
    capability=task.capability,
    expected_result=task.expected_result,
)
```

| 字段 | 要回答的问题 | 示例 |
| --- | --- | --- |
| objective | 要做什么 | 检索规则变更记录 |
| capability | 用哪类能力 | KNOWLEDGE_RETRIEVAL |
| expected_result | 需要得到什么信息 | 变更内容、生效时间、适用模型版本及来源 |

expected_result 的实际消费者包括工具参数构造模型；它帮助查询具体化，也进入历史任务摘要。它不是实际执行结果，不是自动验收通过条件。

“取得变更时间和影响范围”合理；“证明规则变更导致 PSI 异常”预设结论，不合理。如果与 objective 只是重复改写，字段就没有发挥独立作用。

模型填写任务含义；程序补稳定保存的任务 ID 和初始状态。候选可靠保存后恢复复用原 ID，不重新生成另一批任务。

## 11. 正式接受：为什么还要一个事务

模型生成和候选保存完成后，prepare_plan 开始正式接受事务：

```text
重新鉴权、检查锁
    ↓
从数据库重新读取候选及状态
    ↓
核对保存的候选与内存待接受对象一致
    ↓
检查任务预算
    ↓
保存正式计划、接受关联、正式执行代次
    ↓ 原子提交
更新内存 Guard 的 generation
```

这保护三个不变量：

- 正式计划确实来自本次运行可靠保存的候选。
- 保存候选 A，不能接受成 B。
- 正式计划、候选来源、运行代次一起生效，不能留下半套关系。

内存 generation 必须在提交后更新，否则数据库回滚后内存可能仍声称已有正式代次。

## 12. 为什么又 check_runtime、又 authorize_run、又检查 Guard

这几个检查承担不同职责，具体位置见 [check_runtime](../backend/src/risk_platform/workflow/runtime.py#L59)。

| 检查 | 实际关注 |
| --- | --- |
| check_runtime | 上下文类型、身份、超时、案件轮次归属、可选输入一致性、资源权限回调、工具绑定 |
| authorize_run | 数据库权威记录与运行权限的核对 |
| Guard.check_lock | 运行锁连接是否仍有效 |
| Guard.check | 运行锁及当前执行代次，阻止过期执行者提交 |

因此，前一次解释若让人理解为 check_runtime 只比较 State 输入，需要纠正：它也调用 authorize_resource 权限回调，并检查工具绑定。它仍不等于完整工具调用授权。

```python
check_runtime(runtime_context, InvestigationAction.CONTINUE, planning_input)
```

传入第三项时，还会把节点使用的完整调查输入与可信案件输入比较。例如运行案件是 credit-v3，State 却带 loan-v2，就拒绝。

外部模型调用前后、跨事务、恢复时重新检查，是为了防止等待期间权限或运行权变化。不是每一行代码都需要重复检查；相邻且完全相同、没有状态边界的检查可以评估合并。

### 12.1 “这里不是已经检查过了吗？”

是的，这次 check_runtime 已经执行资源权限回调，并比较输入、检查工具绑定。后续如果直接调用下一个函数，使用相同对象，没有等待、输入变化或事务边界，重复执行同样检查未必提供额外保证，不能仅凭“多一层保护”就认定必要。

但是该调用没有检查数据库会话锁和 generation，不能替代 Guard。也不能替代具体工具的参数校验和资源范围授权。

| 两次检查之间发生什么 | 判断方法 |
| --- | --- |
| 只是同步转交同一份参数 | 核对是否重复，可合并同样的不变量检查 |
| 等待模型或工具 | 返回后提交前需要确认权限和运行权仍有效 |
| 恢复检查点或切换轮次 | 需要重新核对正式关联、当前身份和运行权 |
| 从一般任务进入具体工具调用 | 需要该工具的绑定、参数和范围检查 |

本节给出审查标准，不宣称已逐处完成冗余检查清理；本次只编写说明文档，没有修改鉴权代码。

权限是否能立即反映撤销，还取决于回调是否读取最新权限数据。增加调用次数不会让静态白名单自动变成实时权限系统。

## 13. 正式计划如何进入 State

load_accepted_plan 从数据库读取后返回：

```python
{
    "investigation_plan": plan,
    "task_execution_results": [],
    "observation": None,
    "round_complete": False,
    "investigation_status": "OPEN",
}
```

LangGraph 更新返回的对应字段，不是把整个 State 清空。这里清理的是当前轮次的结果和解释；未返回字段保留，上一轮权威历史仍在数据库中。

读取正式计划的数据库事务与 LangGraph 保存检查点不是同一个原子事务。代码通过“正式记录优先、恢复可重新读取”处理两者之间的中断窗口，而不是声称两者同时提交。

## 14. 并发、失败与恢复

| 场景 | 已保存事实与处理 |
| --- | --- |
| 两个执行者同时推进同一案件 | 外层 Runner 运行锁限制并发；事务行锁保护局部检查 |
| 模型返回非法候选 | 按异常分类登记 FAILED，不接受、不执行 |
| 请求超时等捕获异常 | 按当前代码保守登记 UNCERTAIN，不自动视作未调用 |
| 进程直接退出 | 可能遗留 STARTED，下次恢复核实 |
| 候选 COMMITTED 后退出 | 复用候选和任务 ID，再正式接受 |
| 接受事务中出错 | 正式计划、接受关联和代次一起回滚，候选仍保留 |
| 接受提交后、State 更新前退出 | 恢复读取正式计划，不盲目重新调用模型 |
| 输入或协议与已登记规格不同 | 拒绝覆盖，不能把旧候选当新输入的结果 |

_planning_candidate 将 ValueError、ValidationError 分类为 FAILED，其他捕获到的 Exception 分类为 UNCERTAIN；若失败登记本身出错，则不能保证终态已保存。进程终止等也不一定进入异常处理代码。

不重复调用和保证最终完成是两件事。默认阻止不确定重试可能需要明确恢复策略，当前机制不是无条件自动完成系统。

## 15. 与 _phase 的关系，以及当前不能夸大的保证

请求参数生成、工具执行、Observation 使用 phase_execution 的统一阶段机制。规划使用 planning_repository 的规划日志，再加正式接受事务，当前并没有直接调用 _phase。

两者共享“先登记、事务外执行、再提交”的原则，但不能在讲解时说成完全相同的函数调用链。规划必须额外区分候选可靠保存和正式计划接受。

当前边界：

- 契约、预算、关联和权限校验不证明自然语言计划方向正确。
- 历史任务和 Prompt 指令不等于已经实现可靠的语义去重。
- initial_plan 当前没有直接调用 token 请求预算检查或 record_usage；不能据此宣称规划调用已经完整接入这些能力。
- Prompt 的“只规划第一轮知识检索”表述与函数复用于后续轮次、注册能力扩展的现状存在命名和描述不一致，需要后续代码改动时统一。
- 本文是当前实现的阅读说明，不是新增端到端验收；没有执行真实模型、业务数据库迁移或并发恢复测试。

## 16. 一次自己沿代码核对的练习

以“生成两个知识检索任务”为例，依次确认：

1. plan_node 拿到的 call 属于哪一轮？
2. generation 是否为 0，是否需要候选？
3. planning_specification 固定了哪些生成依据？
4. 有没有可复用 COMMITTED 候选？
5. 模型请求前 STARTED 是否已提交？
6. initial_plan 发给模型的材料是什么，返回了什么？
7. task_id、status 由谁填写，expected_result 给谁使用？
8. 候选 COMMITTED 后，为什么仍不能执行？
9. 正式接受事务是否同时写入三个关联事实？
10. 最后返回给图的是哪些字段，其他 State 是否保留？

前两部分入口与 Runner 说明见 [聊天入口与模型决策](s-2026-11-15-chat-entry-app-state-depends-memory-model-decision.md) 和 [确认、执行、Runner 与检查点](s-2026-11-15-confirmation-execution-runner-checkpoint.md)。
