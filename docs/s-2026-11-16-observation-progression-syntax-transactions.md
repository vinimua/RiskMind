# 第五阶段：结果解释、正式 Observation 与下一轮推进

日期：2026-11-16  
关键词：observation、observe_round、observation_phase、advance_round、闭包、partial、事务、结果选择

本文依据当前代码，解释这一阶段的语法、调用逻辑及阅读中提出的问题。示例仅用于理解；本文只新增说明文档，不修改业务代码。

## 1. 先区分三件事

工具执行完成后，需要回答三个不同的问题：

1. 本轮结果能说明什么，还有哪些问题没有解决？
2. 这份解释是否经过校验并可靠保存？
3. 是否有权限和预算继续下一轮？

对应职责如下：

| 函数 | 职责 | 模型调用 | 数据库写入 |
| --- | --- | --- | --- |
| observation | 校验解释输入，组织执行或复用 | 间接，可能跳过 | 通过阶段入口 |
| observe_round | 根据实际提供的材料生成解释草案 | 是 | 不写 |
| advance_round | 接受停止或建立唯一后继轮次 | 否 | 是 |

模型负责建议；正式 Observation 保存解释产物及来源关联，但不保证结论语义一定正确；推进事务决定流程是否继续。

## 2. 完整调用链和跳转

```text
graph.observe_node
    ↓ 取得当前轮次，读取历史材料
nodes.observation
    ↓ 定义 explain，交给持久化入口
phase_execution.observation_phase
    ↓ 构造阶段输入、包装业务产物
_phase
    ├─ 已有正式结果 → 读取复用，不调用解释函数
    └─ 没有结果 → 登记 STARTED，调用解释函数
                       ↓
                 _interpret_observation
                       ├─ 无可用材料 → INCONCLUSIVE
                       └─ 调用 observe_round → 校验草案及引用
    ↓ 结果、采用关系及记忆同事务提交
graph.observe_node
    ↓ advance_round 核对正式结果并保存推进决定
    ↓ 返回合并后的 State 更新
条件边：继续规划，或 END
```

| 位置 | 重点 |
| --- | --- |
| [observe_node](../backend/src/risk_platform/workflow/graph.py#L64) | 组合入口及推进调用 |
| [observation](../backend/src/risk_platform/workflow/nodes.py#L187) | 输入校验和闭包 |
| [_interpret_observation](../backend/src/risk_platform/workflow/nodes.py#L234) | 有无材料、模型失败回退 |
| [observe_round](../backend/src/risk_platform/agents/investigation.py#L44) | 模型输入、响应与引用检查 |
| [observation_phase](../backend/src/risk_platform/workflow/phase_execution.py#L213) | 阶段输入和产物转换 |
| [_phase](../backend/src/risk_platform/workflow/phase_execution.py#L56) | 事务和恢复 |
| [ObservationDraft](../backend/src/risk_platform/workflow/observation.py#L57) | 模型草案契约 |
| [build_observation_input](../backend/src/risk_platform/workflow/observation.py#L189) | 实际提供给模型的材料 |
| [complete_observation](../backend/src/risk_platform/workflow/observation.py#L297) | 正式解释构造 |
| [advance_round](../backend/src/risk_platform/workflow/planning.py#L285) | 确定性推进事务 |
| [条件路由](../backend/src/risk_platform/workflow/graph.py#L20) | 根据 round_complete 选择下一节点 |

行号可能随注释调整，以函数名为准。observation_phase、observe_round、advance_round 不是三个独立图节点；图节点只有 observation。

## 3. 场景：查到规则变更，但还不知道适用版本

```text
原始问题：调查 credit-v3 的异常
第一轮任务：检索规则变更记录、检索版本说明
执行结果：找到变更记录，版本说明零命中
历史解释：尚有此前没有回答的问题
```

模型可以解释已有资料，保留版本问题，并建议继续。它不能因为历史案例类似就确认当前根因，也不能自己批准下一轮预算。

## 4. observation 入口先做什么

入口重新检查可信上下文与原始调查输入的一致性，解析计划和执行结果，再通过 validate_round_inputs 核对本轮任务与结果集合。

```python
plan = _business_model(InvestigationPlan, state["investigation_plan"])
results = [_business_model(TaskExecutionResult, row)
           for row in state["task_execution_results"]]
plan, results = validate_round_inputs(plan, results)
```

列表推导式逐项转换结果。最后一行是元组解包，把函数返回的两个值分别赋给 plan、results。契约检查用于拒绝损坏输入，不证明来源正文或自然语言解释正确。

## 5. explain 闭包逐句解释

```python
async def explain():
    update = await _interpret_observation(
        dict(planning_input=state["planning_input"],
             investigation_plan=plan,
             task_execution_results=results),
        investigation_agent=investigation_agent,
        runtime_context=runtime_context,
        previous_observations=previous_observations,
    )
    return update["observation"]
```

### 5.1 定义不等于调用

async def 定义异步函数；执行到定义时，不会立即解释结果。调用 explain() 得到协程，await explain() 才等待它的执行结果。

函数引用了外层的 state、plan、results 等变量，这是闭包。闭包捕获变量引用，不自动深复制数据；不能据此宣称输入被固定成不可变快照。

### 5.2 dict 是整理材料，不是数据库写入

dict(planning_input=..., investigation_plan=..., task_execution_results=...) 等同于用花括号创建三个键的字典：

- 原始输入：为什么调查。
- 正式计划：本轮准备查什么。
- 执行结果：实际查到什么。

这里只创建字典，没有修改整个 State，也没有提交数据库；其中对象仍是引用。

### 5.3 await 和返回值拆包

await _interpret_observation(...) 等待解释完成，期间协程可以让出执行机会。返回的 update 形如 {"observation": observation_object}。

return update["observation"] 只取解释对象，不把整个 State 更新字典交给账本。键不存在会抛 KeyError；当前辅助函数各返回路径都应提供这个键。

### 5.4 为什么传 explain 而不是 explain()

```python
output = await observation_phase(explain, results=results)
```

传函数让持久化入口先查历史：已有结果直接复用，没有结果才调用。提前执行解释会失去跳过重复模型请求的机会。

## 6. 原始背景、历史 Observation、本轮结果有什么区别

| 材料 | 回答的问题 | 示例 |
| --- | --- | --- |
| 原始调查背景 | 为什么查，目标是什么 | credit-v3 的 PSI 异常 |
| 历史 Observation | 之前查得怎样，还有什么问题 | 已有变更线索，适用版本未知 |
| 本轮工具结果 | 这轮真正取得什么 | 查到某版本的生效说明 |

原始背景防止偏题，历史解释保持连续性，本轮结果提供新的依据。历史 Observation 不是原始执行事实的替代品，也不是当前根因证明。

unresolved_questions 要表达截至本轮仍未解决的问题集合，不能因本轮只处理一部分就丢掉其他历史问题。当前依靠模型输入和契约、Prompt 约束组织这项行为，不能保证自然语言问题集合绝不遗漏。

## 7. _interpret_observation：何时不调用模型

先校验输入并生成执行摘要。如果没有任何 KNOWLEDGE_AVAILABLE 或 RESULT_AVAILABLE，直接构造 INCONCLUSIVE，不调用模型。

有可解释材料时，使用 asyncio.timeout 和 asyncio.to_thread 调用同步 observe_round。计划和结果会深复制，原始调查输入也重新校验和复制。

to_thread 避免同步模型请求阻塞事件循环，不是可靠后台任务。外层超时不保证线程或远程调用立即终止。

模型草案返回后通过 complete_observation 再核对来源，生成正式对象。预算不足、超时、非法结构或引用、依赖失败等被转换为明确的不确定解释；不是伪装成已经分析成功。

当前“无材料”回退构造的 unresolved_questions 为空，不能把正常模型路径的历史问题保留要求，扩大成所有失败回退均保留完整问题列表。

## 8. observe_round：模型到底做什么

### 8.1 构造实际输入

build_observation_input 按预算整理计划、原始背景、历史解释、能力及工具执行结果。模型看到的是筛选后的结果项，不是数据库所有正文。

如果确有工具结果，但没有任何结果项能提供给模型，抛 ContextBudgetExceeded，不让模型在没看到正文时编造解释。

any(...) 判断是否至少有一项满足条件；item.get("items", []) 在字段不存在时用空列表；“省略正文”和“工具零命中”含义不同。

### 8.2 真实调用与用量

```python
response = self.client.chat.completions.create(
    model=self.model,
    messages=[
        {"role": "system", "content": OBSERVATION_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ],
    response_format={"type": "json_object"},
)
record_usage(response)
```

system 提供解释规则，user 提供 JSON 材料。ensure_ascii=False 保留中文字符显示，不改变材料的信任级别。json_object 不是完整业务 schema 保证，仍需 Pydantic 校验。

record_usage 记录平台返回的用量观测，不等于保存业务解释。方法上的 observe_generation 装饰器提供模型调用观测，不授予权限或改变正式接受规则。

choices[0] 是第一个候选回答，不是第一条资料。当前仍按单候选使用，未显式检查列表长度恰好为一。finish_reason 必须为 stop 且正文存在，否则拒绝不完整返回。

### 8.3 模型草案输出

```json
{
  "findings": [],
  "unresolved_questions": ["新规则适用于哪个模型版本？"],
  "action": "CONTINUE",
  "stop_reason": null
}
```

CONTINUE 必须有问题，且 stop_reason 为 null；STOP 必须有停止原因。findings 可以为空，不能为填字段编造发现。草案不填写 Observation ID、时间或执行状态。

### 8.4 引用为什么检查两层

observe_round 用字典和集合推导式建立“execution_id → 已展示 reference_id 集合”，逐条检查模型引用。hasattr 区分通用引用对象和知识工具引用格式，get(... ) or get(...) 取得相应标识。

这层保护“模型只引用实际提供的材料”。complete_observation 进一步按真实结果、工具引用协议核验来源并构造正式解释。两层不能替代自然语言结论正确性判断。

## 9. observation_phase 在哪里，为什么存在

图中使用 partial 预填 sessions、call、model_name，再把函数交给 observation。partial 只绑定参数，不执行模型或数据库操作。

observation_phase 做三件事：

1. 构造 ToolOperationInput，包含案件、轮次、计划、模型、Prompt 与 tokenizer 摘要、执行结果 ID。
2. 把业务解释包装为 ToolArtifact(observation=...)，交给 _phase。
3. 从返回产物中取出 artifact.observation。

内部又有一个 explain 闭包，它只是“包装产物”；nodes 中同名 explain 负责业务解释。两者作用域不同，但同名会增加阅读成本，不能解释成两个模型步骤。

该适配函数不是必不可少的算法步骤；可以内联，但会让业务节点依赖持久化契约和协议摘要。当前保留它的依据是阶段输入构造及产物转换职责，而不是“多一层更专业”。真正查历史、决定复用和管理事务的是 _phase。

## 10. 解释何时保存，记忆何时保存

_phase 先鉴权、检查 Guard 和历史选择，再登记 STARTED；事务外调用解释；返回后再次检查权限和运行权。

正式结果、尝试 COMMITTED、本轮结果选择，以及 capture_observation 的确定性记忆提取在同一个提交事务中完成。解释有来源不等于记忆取得额外权限。

已提交解释可以恢复复用，跳过模型；输入及选择仍需核验。COMMITTED 是可靠提交状态，Observation 内部还可能是 INCONCLUSIVE。

## 11. advance_round 为什么还要读取选择表

核心代码：

```python
selection = await session.get(
    InvestigationRunSelectionRow,
    (identity.run_id, key),
)
if selection is None:
    raise ledger.LedgerIntegrityError("Cannot advance an uncommitted observation")
result_row = await session.get(ResultRow, selection.result_id)
```

session.get 按主键查。括号中的元组是组合主键的两个值：本次 run_id、Observation 操作 key。

```text
选择表：run-1 + observation-key → result-123
结果表：result-123 → 正式解释内容
```

没有选择记录，直接证实的是“本轮没有正式采用关系”，不等于已经查遍结果表证明没有任何解释。代码禁止只凭 State 中有对象就继续。

它不自动取最新结果，因为一个操作可能有多次尝试，本轮必须使用明确选定的那一份。结果原文由账本读取器做契约和摘要校验。

之后还比较数据库 Observation 与 State 中的值：

```python
if observation != read_observation(to_checkpoint_data(state["observation"])):
    raise ledger.LedgerIntegrityError(...)
```

这是值一致性检查，防止“正式保存 A，却拿 B 推进”。它不是重新调用模型，也不是重新证明解释正确。

## 12. advance_round 的确定性算法

在一个 sessions.begin() 事务中先核对权限、Guard 和正式解释。已有 RoundProgressionRow 时核对根运行及来源结果，复用原决定。

没有决定时，按顺序判断：

| 条件 | 动作 |
| --- | --- |
| Observation 不为 SUCCEEDED | 停止，说明仍未解决 |
| action 为 STOP | 使用模型停止原因 |
| 没有未解决问题 | 停止，不宣称确认根因 |
| 轮次、累计任务预算耗尽 | 停止 |
| 时间预算耗尽 | 停止 |
| 没有绑定且注册的工具 | 停止 |

spent 是历史任务数量加本轮结果数量；number 是当前轮次相对本次根轮次的序号。时间在推进边界检查，不是远程调用的硬截止。

reason 为 None 表示这些停止条件尚未命中，不等于模型取得继续权限。代码仍需当前权限和下一轮权限检查。

## 13. 后继身份与事务语法

后继 ID 由根运行与来源轮次计算摘要，再加 round-、run-、plan- 前缀。这使同一来源轮次的重复推进得到稳定身份；摘要本身不证明权限，唯一约束和关联检查仍然必要。

```python
successor = {} if reason is not None else dict(
    next_round_id=...,
    next_run_id=...,
    next_plan_id=...,
)
decision = RoundProgression(..., stop_reason=reason, **successor)
```

三元表达式选择停止或继续字段；`**successor` 把字典展开为关键字参数，不是在执行工具。

允许继续时创建 RoundRecord，使用 replace(call.runtime, round=next_round) 产生新上下文，不原地修改上一轮对象。核对下一轮权限后保存推进记录；失败则新轮次与推进一起回滚。

预留 next_plan_id 不代表已经生成 AcceptedPlan。下一轮仍回到 prepare_plan 生成、校验并正式接受任务。

## 14. State 更新和条件回边

图使用 update.update(await advance_round(state | update, ...))。

- state | update 创建合并字典，右侧同名字段优先，供推进校验使用，不原地改变 state。
- update.update(...) 修改本地更新字典，加入推进结果。
- 继续时推进结果会把 observation 清为 None，为下一轮准备；正式本轮解释已经在数据库保存。

允许继续时返回下一轮 run_id、plan_id，清理当前计划及结果视图，round_complete=False。停止时 round_complete=True、保存 stop_reason，案件仍为 OPEN。

条件边只按 round_complete 路由，不调用模型，不重新计算预算：False 回 prepare_plan，True 到 END。字段缺失默认 False，因此它依赖前面的节点和检查点校验，不负责单独证明状态完整。

END 结束本次图调用，不等于关闭案件或确认根因；原轮次计划、结果、解释和推进记录不覆盖。

## 15. 中断恢复为什么分成两个提交

Observation 与推进决定分别提交，不是一个覆盖模型等待的大事务。

| 中断点 | 恢复语义 |
| --- | --- |
| 模型调用后，解释尚未可靠提交 | 可能留下 STARTED，按不确定尝试处理 |
| 解释已提交，推进未保存 | 复用解释，再建立推进决定 |
| 推进已提交，图检查点未更新 | 复用原推进，不能创建第二个后继 |
| 新轮次已批准，计划未正式接受 | 继续规划与接受流程，不执行未经接受的任务 |

运行权跨轮保持，轮次身份独立。权限回调是否及时反映撤销取决于实际实现，重复检查不能把静态配置变成实时权限系统。

## 16. 阅读时需要记住的边界

- 草案合法、引用存在、数据库提交成功，都不等于当前根因已确认。
- 模型建议 CONTINUE 不等于程序已批准继续；STOP 也不等于案件业务已成功解决。
- observation_phase 是阶段适配，_phase 是可靠执行协议，advance_round 是业务推进决定。
- 保留必要事务和运行权检查，不意味着所有相邻重复校验都必需；现有重复及命名仍可继续改进。
- 本文核对的是当前代码，不新增能力、不执行真实模型或业务数据库迁移。本次仅验证文档链接、示例和格式。

前一阶段见 [第四阶段：任务执行、工具校验与恢复](s-2026-11-16-task-execution-tool-validation-ledger-recovery.md)，下一轮规划见 [规划、正式接受与 State 恢复](s-2026-11-16-planning-candidate-acceptance-state-recovery.md)。
