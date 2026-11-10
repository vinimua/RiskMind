# prepare_plan：设计、代码、事务与恢复讨论整理

本文按当前实现整理讨论，解释已有机制；本文没有修改业务行为或新增恢复能力。

## 1. 函数究竟做什么

`prepare_plan()` 保证任务执行节点得到一份已经正式接受、可靠保存的计划，然后返回 State 更新。

```text
尚未绑定正式执行代次
→ 取得可靠候选 → 正式接受 → 加载正式计划

已经绑定正式执行代次
→ 直接加载正式计划
```

这里没有独立意图识别。当前调用方已经决定发起或继续调查，才进入 Runner。普通问答、查看进度、发起调查的统一意图分流尚未实现，也不属于计划接受事务。

## 2. 对应代码在哪里

| 文件 | 重点函数 | 职责 |
|---|---|---|
| [runner.py](../backend/src/risk_platform/workflow/runner.py) | `InvestigationRunner.ainvoke()` | 入口授权、身份、运行锁、检查点 |
| [graph.py](../backend/src/risk_platform/workflow/graph.py) | `plan_node()` | 唯一 LangGraph 的计划节点 |
| [planning.py](../backend/src/risk_platform/workflow/planning.py) | `prepare_plan()`、`_planning_candidate()` | 候选生成/复用、正式接受、加载 |
| [investigation.py](../backend/src/risk_platform/agents/investigation.py) | `initial_plan()` | 模型生成草案，转成 InvestigationPlan |
| [planning_repository.py](../backend/src/risk_platform/workflow/persistence/planning_repository.py) | `register/history/start/settle/candidate/validate` | 规划输入、尝试及候选的数据库操作 |
| [run_guard.py](../backend/src/risk_platform/workflow/run_guard.py) | `_claim()`、`RunGuard`、`_bind_execution_run()` | 运行权与执行代次 |
| [access.py](../backend/src/risk_platform/workflow/access.py) | `authorize_run()` | canonical 归属及 READ、CONTINUE 授权 |
| [planning_models.py](../backend/src/risk_platform/workflow/persistence/planning_models.py) | 三张规划表 | 规划输入、尝试、接受来源约束 |

## 3. 术语对照

| 对象或变量 | 是什么 | 不代表什么 |
|---|---|---|
| `RunIdentity` | run / investigation / round / plan ID | 有预留 plan_id 不代表已有计划正文 |
| `spec: PlanningInput` | 规划模型、输入和提示词摘要，附带运行归属 | 不是模型生成的任务计划 |
| `PlanningCandidate` | 候选计划包装 | 尚未成为正式执行计划 |
| `attempt_id` | 一次规划尝试编号 | 不是线程 ID、task_id 或 plan_id |
| `accepted` | 已校验、准备接受的 AcceptedPlan 类型对象 | 变量名不证明已经正式接受入库 |
| `fact` | 数据库里的规划尝试记录 | 不是一条新生成的计划 |
| `expected` | 从数据库候选重新校验构造的 AcceptedPlan 对象 | 不是重新调用模型的结果 |
| `COMMITTED` | 候选产物已经可靠保存 | 不等于正式接受，更不等于调查完成 |

$$
\text{Candidate COMMITTED} \not\Rightarrow \text{Plan Accepted}
$$

## 4. 从应用到 prepare_plan

```text
可信应用提供 runtime
→ Runner 鉴权、确定运行身份、取得运行会话锁
→ 创建 RunExecutionContext(identity, policy, guard)
→ 包装为 InvestigationInvocation(runtime, execution)
→ 调用唯一 LangGraph
→ plan_node 调用 prepare_plan
```

`prepare_plan` 的 `invocation` 参数是内部 RunExecutionContext，不是外层 InvestigationInvocation。`runtime` 则作为显式参数提供。

```python
async def prepare_plan(state, invocation, *, sessions, agent, runtime):
```

- `state`：当前业务状态；可信调查输入来自 runtime 中的正式调查记录。
- `invocation`：运行身份、恢复策略和运行护栏。
- `sessions`：数据库会话工厂。
- `agent`：初始规划 Agent。
- `runtime`：当前身份、案件、轮次、授权入口和工具绑定。
- `*`：后面的参数必须使用名称传入。

## 5. generation 不负责查候选

```python
if invocation.guard.generation == 0:
```

它表示当前尚未绑定正式执行代次，需要进入计划准备、接受流程。不表示没有运行锁，也不表示没有候选。

```text
generation == 0
├─ 没有候选：生成
├─ 有 COMMITTED 候选：复用
└─ 有旧未完成尝试：按恢复规则与策略处理
```

是否存在候选，由 `_planning_candidate()` 查询 `planning.history(session, run_id)` 后判断。`planning_specification()` 本身不查询数据库。

generation 大于 0 时，当前调用已经绑定正式执行代次，直接加载已有正式计划。

## 6. planning_specification 的用途

它构造 PlanningInput，记录：运行归属、初始规划模型、调查输入摘要、初始规划提示词摘要。

目的：恢复时判断旧候选是否仍对应同一份规划输入。实际登记、比较发生在 `planning.register()`。

$$
D_{input}=\operatorname{SHA256}(\operatorname{CanonicalJSON}(input))
$$

$$
D_{prompt}=\operatorname{SHA256}(\operatorname{UTF8}(prompt))
$$

摘要用于比较输入变化，不是原文、自动防篡改系统或模型输出一致性的保证。列表顺序和业务内容变化仍可能改变摘要。

这只固定已登记的初始规划阶段输入，不固定后续 Query、Observation 的模型配置。

## 7. accepted 如何得到

首次生成：

```text
_planning_candidate()
→ nodes.initial_planning()
→ agent.initial_plan()
→ InvestigationPlan
→ PlanningCandidate
→ planning.validate()
→ 内存 AcceptedPlan 对象
```

`validate()` 检查目标、预期结果、任务能力等规则，并绑定可信的案件、轮次和计划身份；它不写正式计划表。

复用候选：

```text
history 找到 COMMITTED 尝试
→ planning.candidate() 解析落库候选、检查摘要
→ planning.validate()
→ 内存 AcceptedPlan 对象
```

因此 `attempt_id, accepted = await _planning_candidate(...)` 是两个返回值的解包。真正写正式计划的语句是后面的 `ledger.accept_plan(session, accepted)`。

## 8. 完整事务顺序

事务 1、2、3 属于 `_planning_candidate()`；事务 4 是 `prepare_plan()` 中正式接受事务。

```text
事务 1：授权、检查锁、登记/核对规划输入、查询历史
        已有 COMMITTED → 复用返回，不再调用模型
        旧 STARTED → 检查占用来源，必要时结算 UNCERTAIN
提交
  ↓
事务 2：授权、检查锁、检查重试策略、保存新 STARTED
提交
  ↓
事务外：等待模型，生成并校验候选
  ↓
事务 3：授权、检查锁、保存候选及 COMMITTED
提交
  ↓
return attempt_id, accepted
  ↓
事务 4：重新读取候选 → 正式计划 + 来源关联 + 运行代次
提交
  ↓
更新内存 guard.generation
  ↓
从数据库加载正式计划，交给后续节点
```

这是顺序执行：事务 3 成功提交、函数返回以后，才会开始事务 4。

## 9. 原子性保证的范围

`async with sessions.begin()` 正常退出提交，抛异常回滚。`add()` 或 `flush()` 都不等于最终提交。

| 事务 | 原子提交的内容 |
|---|---|
| 事务 3 | 候选正文、摘要与尝试状态 COMMITTED |
| 事务 4 | 正式计划及任务、候选来源关联、正式运行及执行代次记录 |

两个事务之间没有整体原子性。“候选已提交、正式计划尚未接受”是允许存在且能够恢复的中间状态。

数据库不能撤销模型调用，也不能退回费用。模型已执行但结果没有可靠保存时，可能只能记录 UNCERTAIN，不能断言未执行。

## 10. 接受事务里的检查分别做什么

### 10.1 authorize_run

```python
trusted = await authorize_run(session, runtime)
```

核对数据库 canonical 案件和轮次归属，分别检查 READ、CONTINUE。两项都需明确允许。

```text
READ：允许读取案件及历史
CONTINUE：允许继续推进调查
```

`trusted` 是经过核对的上下文，不是布尔值。这不等同于知识工具的 `knowledge:search` 授权。

等待模型期间可能撤权，所以接受前重新检查有价值；但检查后、提交前仍可能发生撤权。当前没有承诺与外部权限系统形成原子撤权协议。

### 10.2 check_lock

```python
await invocation.guard.check_lock()
```

确认原来的专用连接仍有效并持有运行会话锁。它检查执行权，不检查用户权限；此时尚未建立正式代次，因此不是完整的 generation 对账。

### 10.3 查询候选来源

```python
facts = await planning.history(session, invocation.identity.run_id)
fact = next((row for row in facts if row.attempt_id == attempt_id), None)
```

`history()` 使用 FOR UPDATE，保护事务内的尝试行检查。`next(..., None)` 表示找到第一条编号匹配的记录，找不到返回 None。

### 10.4 COMMITTED 检查不是再次保存

```python
if fact is None or fact.status != "COMMITTED":
    raise ledger.LedgerIntegrityError(...)
```

这里只检查。候选已经在事务 3 中保存，正式接受事务不重复生成或保存候选。

当前正常路径中，事务 3 提交成功才返回，且存在运行锁和已提交记录不可变约束，这项检查不应触发。它是接受边界的防御性断言，针对错误编号、代码缺陷、越权修改或测试注入损坏；不是常规多线程竞争。

事务 3 提交失败时会直接抛异常，不会正常返回后靠这条 if 补救。

### 10.5 expected 与 accepted 比较

```python
expected = planning.candidate(fact, spec, source)
if expected != accepted:
    raise ledger.LedgerIntegrityError(...)
```

expected 来自数据库原文重验；accepted 来自函数返回。比较是为了发现落库对象与内存对象错配。

正常流程应当一致。不能把“保存 A、接受 B”描述成经常自然出现的并发情况。

可简化方向是：候选函数只返回 attempt_id，接受时从数据库构造唯一的正式计划对象，从而消除双份对象比较。**这个简化尚未实施。**

### 10.6 正式接受与运行绑定

```python
row = await ledger.accept_plan(session, accepted)
session.add(PlanningAcceptanceRow(...))
generation = await _bind_execution_run(...)
```

三项分别记录：正式计划、由哪次候选和谁接受、由哪个运行和代次执行。它们同事务提交。

`_bind_execution_run()` 不执行计划任务，也不再次取得运行锁。它新建或核对运行身份，推进 current_generation，保存本次执行者与策略。

退出事务成功后才执行 `invocation.guard.generation = generation`，避免数据库回滚而内存提前宣称成功。

## 11. 事务、行锁、运行锁、generation 的区别

| 机制 | 目的 | 结束时机 |
|---|---|---|
| 数据库事务 | 一组数据库写入一起提交或回滚 | 当前事务结束 |
| FOR UPDATE 行锁 | 检查/更新记录时防止并发修改 | 当前事务结束 |
| advisory 运行锁 | 同一个 run 同时只由一个执行者推进 | 专用持锁会话释放或失效 |
| generation | 识别旧执行者，拒绝过期提交 | 作为数据库与内存中的代次分别保存 |

advisory lock 是合作协议，不会自动锁住所有业务表行。事务提交释放行锁，但外层专用连接可以继续持有运行锁。

因此模型等待期间没有持续开启这些账本短事务，仍有运行互斥保护。

## 12. 这里是不是多线程

同一次调用中，事务 3 和正式接受事务不会并行。`await _planning_candidate(...)` 会等待完成才继续。

`asyncio.to_thread()` 只是把同步 Agent 调用移到工作线程，代码仍 await 结果；它不自动产生新的调查运行或代次。

需要防止的是两个独立请求同时推进同一个 run：可能来自同线程异步任务、不同线程、进程或服务器。单线程也会在等待 I/O 时交错处理请求。

```text
连续点击两次
→ A 取得 RUN_01 的锁并执行
→ B 取锁失败，拒绝重复推进
```

不同 run 可以并行。超时或取消等待也不保证工作线程里的外部调用已经停止，因此中断不总能当成明确失败。

## 13. generation 会不会随线程重置

不会。内存 guard.generation 是本次执行者持有的代次，数据库 current_generation 是当前正式代次。

$$
g_{new}=\operatorname{COALESCE}(g_{old},0)+1
$$

```text
A 持有代次 1，等待模型
→ A 持锁连接断开
→ B 接续同一个 run，数据库代次推进为 2
→ A 返回，手中仍是 1
→ 完整 guard 检查发现不匹配，拒绝旧提交
```

规划接受前可以是 0，接受后建立正式代次。另一个全新 run 有独立代次。新请求必须先取得运行权，不能在 A 正常持锁时直接拿到下一代。

## 14. 案件、轮次为什么还要核对

边界检查防止错误参数或共享可变对象造成“案件 A + 属于案件 B 的轮次”，也防止传入篡改 ID。

正常可信加载路径中，这类错误应很少发生。线程切换不会随机把对象换成另一个案件。

外键保证数据库记录的合法关联，不能单独证明调用者传入的上下文就对应这些记录；入口和恢复边界仍要核对。

但是“归属校验必要”不等于“同一事务重复加载、重复校验每次都不可省”。未来可以梳理合并，不能为每项冗余都制造常见故障理由。

## 15. 故障场景与预期行为

| 场景 | 保存状态 | 处理 |
|---|---|---|
| 首次正常完成 | 候选 COMMITTED、正式计划、来源及运行代次 | 加载正式计划，进入任务执行 |
| 候选已提交、尚未接受 | 有可靠候选，无正式计划 | 直接复用，不再次调用模型 |
| 模型执行后、事务 3 提交前中断 | 可能只有 STARTED | 按旧占用与显式策略处理 UNCERTAIN |
| 事务 3 提交后崩溃 | COMMITTED 候选 | 下次复用并完成接受 |
| 接受事务某项写入失败 | 接受事务全部回滚，候选仍在 | 保留候选，后续可重入 |
| 接受提交后、内存或 checkpoint 更新前崩溃 | 正式计划与运行已存在 | 下次从数据库识别并加载 |
| 两个请求同时推进同一 run | 一方持有运行锁 | 另一方拒绝，不重复推进 |
| 等待模型期间丢失持锁连接 | 旧进程已失去执行权 | 后续锁检查拒绝写入 |
| 重新鉴权观察到撤权 | 已提交事实仍保留 | 拒绝后续未授权步骤 |
| 内存候选与落库候选错配 | 原候选仍保留 | 完整性断言报错 |

FAILED、UNCERTAIN 和零命中不是同一种结果。重试须由显式策略决定，不能用“重来一次看看”隐藏未知调用。

## 16. 返回 State 与当前边界

`load_accepted_plan()` 从 PostgreSQL 读取正式计划，校验后转换为 JSON 可保存结构，返回计划、空任务结果、空 Observation、round_complete=False、investigation_status=OPEN。

它只是为后续任务执行准备状态，不关闭案件、不确认根因、不执行真正多轮 Replanning。

当前运行和规划输入只支持 v2，旧格式会被拒绝。Operation、Result 等其他契约各有版本，不能把所有 v1 一概视为同一种旧数据。

## 17. 必需目标和可简化实现

| 内容 | 判断 |
|---|---|
| 正式任务需要可靠计划来源 | 核心目标 |
| 同一 run 避免并发重复推进 | 核心目标，运行锁是当前方式 |
| 数据库写入事务一致性 | 核心目标 |
| 外部调用中断保留不确定性 | 核心目标，不能靠数据库回滚解决 |
| 候选与正式计划分两个事务 | 不是唯一方案，当前用于明确恢复边界 |
| expected/accepted 双份对象比较 | 防御性实现，可通过单一数据来源简化 |
| 每个内部步骤重复校验同样对象 | 不应一概认定必需，应按边界梳理 |

阅读时先抓住主线：**可靠候选 → 正式接受 → 加载正式计划**，再分别跟踪事务、授权和运行权。不要把每项防御性检查都理解为正常流程经常会发生的异常。
