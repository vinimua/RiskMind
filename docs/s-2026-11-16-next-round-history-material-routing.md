# 第六阶段：下一轮身份、历史材料与规划回边

日期：2026-11-16  
关键词：current_round_call、round_material、prepare_plan、推进记录、历史任务、条件回边、检查点

本文按当前代码整理正常路径和恢复路径，并纳入阅读中的追问。示例编号仅用于理解。本次只编写文档，不修改实现，也不把改进建议描述成已完成能力。

## 1. 场景：第一轮没查完，第二轮继续

```text
原始问题：调查 credit-v3 的异常
第一轮发现：近期有规则变更
未解决：变更适用于哪个版本？什么时候生效？
    ↓
advance_round 核验正式 Observation、权限和预算
    ↓
批准第二轮，保存后继身份
    ↓
条件边回到 prepare_plan
    ↓
确认第二轮身份 → 读取历史材料 → 生成并正式接受新计划
```

继续不是重做第一轮，也不是直接执行模型新写的任务。每轮有独立身份和正式计划，上一轮记录不覆盖。

## 2. 四处代码负责四个问题

| 位置 | 负责回答 |
| --- | --- |
| [current_round_call](../backend/src/risk_platform/workflow/planning.py#L197) | State 指向的轮次是否属于正式推进链？ |
| [round_material](../backend/src/risk_platform/workflow/planning.py#L251) | 以前取得什么、做过什么、还剩多少任务预算？ |
| [prepare_plan](../backend/src/risk_platform/workflow/planning.py#L69) | 本轮正式批准执行哪些任务？ |
| [条件路由](../backend/src/risk_platform/workflow/graph.py#L20) | 回到规划，还是结束本次图调用？ |

条件回边不是第四个业务函数，而是路由函数和 add_conditional_edges 的组合。图节点仍是 prepare_plan、task_execution、observation。

## 3. 顺序：先批准，再回边，再生成计划

[observe_node](../backend/src/risk_platform/workflow/graph.py#L64) 先完成并提交解释，再调用 [advance_round](../backend/src/risk_platform/workflow/planning.py#L311)。

允许继续时，advance_round 保存新的 round_id、run_id、plan_id，并返回 round_complete=False。它只批准后继轮次和预留计划身份，不生成 AcceptedPlan。

图回到 [plan_node](../backend/src/risk_platform/workflow/graph.py#L49)，先调用 current_round_call，再调用 prepare_plan。后者才负责本轮候选、校验和正式接受。

```text
批准下一轮 ≠ 生成下一轮任务 ≠ 正式接受计划 ≠ 执行任务
```

## 4. current_round_call：沿正式后继链找目标

### 4.1 输入与起点

```python
target = state.get("run_id", root.execution.identity.run_id)
identity = root.execution.identity
```

State 给出待核对的目标；root 给出本次图调用最初取得的可信起点和运行控制。get 的默认值只在键不存在时使用，不会把显式 None 当成缺省值。

### 4.2 查找算法

```text
当前身份 = root
while 当前运行不是目标运行：
    检查是否重复访问
    读取当前轮次的 RoundProgression
    核对根运行、来源运行、案件和后继
    当前身份 = 数据库记录的后继身份
```

例子：root 为 run-1，目标为 run-3，必须核对 run-1 → run-2 → run-3，不能只因为 State 写了 run-3 就赋予该运行身份。

visited 是集合，用于检测环；缺少正式后继记录、跨案件或根运行不匹配时抛完整性错误。到达目标后再核对 plan_id。

若走过 R 个轮次，链遍历成本大致 O(R)，visited 空间也随已访问轮次增长；数据库权限检查和查询还有额外成本。

### 4.3 返回什么

仍在根轮次就返回 root。切轮后从数据库读取 RoundRecord 和运行记录，用 replace 创建本轮 runtime，再建立本轮 RunExecutionContext。

运行锁继续使用原连接；run_id、round_id、plan_id、generation 对应本轮。generation 是执行代次，不是轮次序号；没有正式运行记录时为 0。

replace 不原地修改 root；可信上下文不会因为 State 改 ID 就自动变成另一案件。

## 5. “DevF 不就是直接读 State 吗？”

是的，DevF 聊天图直接读取 pending_tool_calls、step 等，见 [工具节点](../DevF/backend/app/services/agents/chat_agent.py#L618)。该聊天图 compile 未配置持久化 checkpointer，不在这里实现同样的正式轮次身份链。

但 DevF 也有工具注册与 Skill 范围检查，不能说它完全不校验。RiskMind 也会直接读取普通业务 State；额外核对的是会影响正式结果归属、权限和恢复的运行身份。

必要的是身份关联不变量，不是“越严格越好”。每个节点从 root 重新遍历是否有冗余，仍值得审查；本文不声称当前实现已达到最小查询量。

## 6. round_material：从哪里找历史

查询核心是：

```python
select(RoundProgressionRow).where(
    RoundProgressionRow.investigation_id == 当前案件ID,
    RoundProgressionRow.next_round_id.is_not(None),
).order_by(RoundProgressionRow.created_at)
```

session.scalars 提取查询返回的 ORM 对象；list 将结果收集成可遍历列表。links 只是变量名，不是轮次表的名称。

注意当前范围：按案件全部“有后继”的推进记录、创建时间排序。代码没有沿 root 的祖先链限定历史，也没有只筛选当前轮次之前的记录。不能把它描述成严格的“本次运行祖先集合”；多根运行或回看较早轮次时，这个范围需要专门审查。

## 7. “为什么是推进记录，不直接读轮次？”

两者表达不同事实：

```text
RoundRow：第一轮存在，属于 case-1
RoundProgressionRow：第一轮采用 result-123 的 Observation，决定进入第二轮
```

推进记录包含来源轮次、来源运行、正式采用的 Observation 结果 ID，以及后继身份或停止原因。

代码从它开始，是为了找到“哪些已完成解释被用于批准继续”，而不只是枚举存在过的轮次：

```text
推进记录 → observation_result_id → 正式解释 → 任务及结果
```

这不是唯一可行查询方式，也可以联合轮次、运行和结果选择表读取。当前利用了已有权威关联。

next_round_id 非空过滤掉停止记录；因此纳入的是批准继续的来源轮次。在新轮次规划的正常路径中，上一轮已经批准继续，恰好有该记录。

## 8. 外层循环：读取来源轮次的正式材料

对每条 link：

1. 读取 source_run_id 对应身份和 RoundRecord。
2. 构造历史轮次 runtime，重新检查读取与运行权限。
3. 通过 observation_result_id 读取结果及阶段输入。
4. 经既有历史结果授权路径读取产物。
5. 找到匹配的 Observation，缺失则拒绝。
6. 把解释加入 observations，并读取来源轮次 AcceptedPlan。

这里 next((...), None) 表示取第一个满足条件的产物，没有则返回 None。ledger._read 会做契约与摘要检查，但不能因此证明自然语言发现正确。

## 9. “已经遍历轮次，为什么还要 zip？”

因为一轮里有多个任务。外层确定哪一轮，内层整理这一轮每个任务的历史事实。

```python
for task, summary in zip(
    plan.tasks,
    artifact.observation.execution_summaries,
    strict=True,
):
    ...
```

| 正式任务 | 对应执行摘要 |
| --- | --- |
| T1：查规则变更 | E1：成功，有资料 |
| T2：查版本说明 | E2：成功，零命中 |

zip 按位置配对，task、summary 是解包后的两个对象。strict=True 检查两个序列数量相同，不会自动按 ID 联表，也不会自己证明顺序正确；配对关联依赖上游执行摘要构造及校验。

内层通过 summary.execution_id 读取实际执行结果，既知道“原来要做什么”，也知道“最终做成怎样”。

## 10. 每条历史任务摘要包含什么

核心字段包括 task_id、objective、expected_result、execution_id、status、outcome、tool_name；有错误则加 error，有请求或检索快照则按载荷添加。

执行操作中的 candidate_request 存在时，代码以其作为实际请求材料，不让模型重写调用事实。当前仍有知识载荷快照字段和缺失工具名回退，不能称为历史整理已经完全消除工具特例。

status 和 outcome 不应混为一谈：

- SUCCEEDED + NO_HITS：工具成功，但没有找到材料。
- FAILED：工具工作失败。
- REJECTED：调用被拒绝。

请求参数和数据快照帮助模型判断是否值得重查。仅凭任务文字相似不能断言无增量价值；向模型提供这些字段也不等于已经实现确定性的语义去重。

## 11. “是不是每次从头总结所有轮次？”

当前是重新遍历查询出的历史记录、组装结构化材料，但不重新调用模型总结每一轮。

最终返回：

```python
dict(
    previous_observations=tuple(observations),
    previous_tasks=tuple(tasks),
    remaining_task_budget=invocation.max_tasks - len(tasks),
)
```

tuple 形成序列，不能据此宣称内部对象递归不可变。没有历史 Observation 时返回空字典。

预算按任务数量扣除，不是成功数，也不是模型调用次数。每轮 R、总历史任务 T 时，材料组装大致 O(R+T)；存在循环内逐项数据库读取及历史查询，实际 SQL 往返更多，不能称为一次批量查询。

## 12. “为什么不用旧材料加本轮新增？”

可以采用增量方式，但当前没有实现。

```text
当前：每次从权威账本重新组装历史
增量：旧结构化材料 + 尚未纳入的新正式结果
```

当前方式减少额外累计材料的维护，恢复可重建；代价是重复读取与组装。

若改增量，需要同时解决：恢复时不重复追加、提交后能补遗漏、已解决问题更新、来源可核对、当前权限仍满足。不能只拼接自然语言字符串，也不能把未经核验的 State 摘要变成权威事实。

可讨论的改进方向是账本保持权威，正常路径维护结构化材料，恢复核对已包含的正式结果，只补缺失部分；但先要明确根运行与历史范围。本节是方案比较，不是新增缓存或快照的授权设计，也不是已完成实现。

## 13. prepare_plan：复用同一规划和接受机制

当前轮次 generation 为 0 时：读取 round_material，建立生成规格，调用 _planning_candidate，生成或复用可靠候选。模型从未解决问题集合选择子集，生成多个任务，不使用单一 next_focus 限制。

随后在正式接受事务中重新检查权限、锁、数据库候选、任务预算，原子保存计划、候选接受关联和运行代次。

已有正式代次则跳过候选生成。两条路径最后都从正式数据库读取本轮计划，返回 State 更新；不会把模型 JSON 直接当正式任务执行。

generation=0 不表示没有运行锁。计划 ID 早已预留也不表示 AcceptedPlan 已建立。详细生成与接受边界见 [规划文档](s-2026-11-16-planning-candidate-acceptance-state-recovery.md)。

## 14. 条件回边：只有一次布尔选择

```python
return "round_complete" if state.get("round_complete", False) else "continue"
```

add_conditional_edges 将 continue 映射为 prepare_plan，将 round_complete 映射为 END。算法本身是 O(1) 的分支，不调用模型、不计算预算、不创建轮次。

真正批准发生在 advance_round；路由消费程序返回的 round_complete，不直接消费模型 action。因此 CONTINUE 建议不代表无条件继续。

字段缺失默认 False，路由本身不做完整性保证，必须依赖前面的节点和恢复校验。

## 15. 为什么一条检查点链能容纳多个运行 ID

[Runner](../backend/src/risk_platform/workflow/runner.py#L77) 把根 run_id 放进 configurable.thread_id，整个图循环保持这个定位不变。它不是操作系统线程，也不是聊天 conversation_id。

State 的 run_id、plan_id 可以切换到当前轮次；可信执行上下文通过 current_round_call 重新解析。不能把“检查点定位稳定”理解成每轮共用同一个执行身份。

```text
稳定 thread_id：root-run
    第一轮：run-1 / plan-1
    第二轮：run-2 / plan-2
    第三轮：run-3 / plan-3
```

外层运行锁覆盖推进过程；各轮正式计划、执行结果、Observation 和推进关系独立保存。正常路径连续执行，不要求人为停顿。

## 16. 中断时哪些事实可以复用

| 中断位置 | 恢复依据 |
| --- | --- |
| 推进提交前 | 已选正式 Observation；重新决定是否可继续 |
| 推进提交后、检查点更新前 | 复用已保存推进，不创建第二个后继 |
| 新轮次规划调用后、候选未提交 | 遗留尝试按不确定策略处理 |
| 候选已提交、正式接受前 | 复用候选及任务 ID，继续接受 |
| 正式计划提交后、State 更新前 | 读取正式计划，避免重新规划 |

数据库业务事务和 LangGraph 检查点不是同一个提交；恢复靠正式记录关联和核验衔接，不是宣称两个存储原子提交。

旧阶段协议记录不再自动降级续跑，旧拓扑检查点也不能静默跳过节点；需要显式迁移，原文和历史读取保留。

## 17. 停止后用户能看到什么

[_progress](../backend/src/risk_platform/entrypoints.py#L102) 按案件查询最新 round_number，再检查读取权限，读取该轮正式计划、选定执行结果、Observation 状态和推进 stop_reason。

它返回简短进度和停止原因，不返回完整知识正文或发现报告。暂无已提交结果不等于工具从未被调用。案件仍为 OPEN，停止不代表已确认根因或案件已关闭。

## 18. 读代码时的四个检查点

1. 轮次身份：State 的目标是否能从正式关系证明？
2. 历史范围：读的是当前根运行历史，还是整个案件的推进记录？
3. 接受边界：只是有候选，还是正式计划已经提交？
4. 路由结果：是模型建议，还是程序已保存的推进决定？

当前需要继续审查的实现边界包括历史读取范围、循环内查询、重复身份解析及顺序配对依赖；本文明确记录这些边界，不以“更严格”解释全部复杂度。

前一阶段见 [Observation、推进和语法](s-2026-11-16-observation-progression-syntax-transactions.md)。本次文档仅验证代码引用、示例和格式，不新增真实模型、迁移或性能验收。
