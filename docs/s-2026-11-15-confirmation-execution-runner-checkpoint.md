# 从点击确认到运行调查：RiskMind 的动作登记、案件创建与 Runner 恢复链路

日期：2026-11-15。依据：当前仓库代码。本文接续[第一篇：聊天入口、身份、历史记忆与模型决策](s-2026-11-15-chat-entry-app-state-depends-memory-model-decision.md)。

第一篇回答“用户发来一句话，系统如何保存消息并取得模型建议”。这一篇回答“建议保存以后，用户确认的是什么，案件何时创建，谁调用 Runner，以及程序中断后按什么依据继续”。文中的示例 ID 用于说明关联，不是真实运行数据。代码节选省略无关行，不代表新增实现；本次只编写文档。

## 1. 先把两种入口和两种 ainvoke 分开

当前对调查 Runner 的业务调用集中在 `_run_case()`。聊天与告警分别构造输入，随后汇入同一条调查执行路径。

```text
聊天入口：
POST /chat/messages
  → 模型提出 start_investigation
  → 保存 PENDING_CONFIRMATION
  → POST /chat/turns/{turn_id}/confirmation
  → EntryServices.confirm_chat()
  → confirm_entry()：保存确认
  → execute_entry()：鉴权、领取动作
  → _create_case()：建立案件与第一轮
  → _run_case()
      → InvestigationRunner.ainvoke()
          → self._graph.ainvoke()

告警入口：
POST /investigations/from-alert
  → EntryServices.from_alert()
  → _create_case()
  → _run_case()
      → 同一个 InvestigationRunner.ainvoke()
          → 同一张调查图
```

`InvestigationRunner.ainvoke()` 是本项目的方法，负责运行身份、权限、锁和检查点核验；`self._graph.ainvoke()` 是已编译 LangGraph 的方法，负责按图执行节点。两者同名，但对象、职责和调用层次不同。

查询进度不走这条运行链：`get_investigation_progress` 在 `execute_entry()` 中调用 `_progress()`，不启动 Runner。

## 2. 确认接口：用户提交的是原建议的确认事实

入口：[HTTP confirm_chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:77)。

```python
@router.post("/chat/turns/{turn_id}/confirmation")
async def confirm_chat(turn_id: str, body: EntryConfirmation,
                       request: Request, actor: str = Depends(principal)):
    return entry_response(
        await invoke(
            request.app.state.entry_services.confirm_chat(
                turn_id, body, actor=actor,
            )
        )
    )
```

沿用第一篇的语法：`Depends(principal)` 取得认证身份，`app.state.entry_services` 取得服务实例。这里的 `turn_id` 来自 URL，不是模型重新生成的编号。

确认体对应 `EntryConfirmation`：

```python
class EntryConfirmation(MemoryContract):
    invocation_id: Identity
    decision_digest: NonBlank = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = Field(strict=True)
```

三个字段各有用途：

| 字段 | 表达什么 | 由谁核对 |
|---|---|---|
| `invocation_id` | 用户确认哪一次服务端入口调用 | 与已保存动作记录比较 |
| `decision_digest` | 用户确认的是哪一版已保存决定 | 与原决定摘要比较 |
| `confirmed` | 同意还是取消 | 必须是真正布尔值，再保存成确认事实 |

确认请求不包含新工具名称、新模型目标或 actor。用户确认 credit-v3 的启动建议之后，执行时读取的是数据库中的原决定，不能在确认体里替换为另一个模型。

这里确认的是“启动这次调查”，不是批准模型根因、训练、发布或后续所有业务权限。调查中仍会执行权限、预算和阶段校验。

## 3. 两个名字相近的方法：confirm_chat 与 confirm_entry

代码：[EntryServices.confirm_chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:137)。

```python
async def confirm_chat(self, turn_id, confirmation, *, actor):
    view = await self.confirm_entry(turn_id, confirmation, actor=actor)
    if view.status == "READY":
        return await self.execute_entry(turn_id, actor=actor)
    return view
```

它是应用层衔接：先保存确认，只有返回状态为 READY，才执行原调用。取消、已有终态或已开始状态不会因此重新执行。

代码：[confirm_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:204)。它自己不运行工具：

```python
confirmation = EntryConfirmation.model_validate_json(
    confirmation.model_dump_json()
)
async with self.sessions.begin() as session:
    turn = await actions.owned_turn(
        session, turn_id, actor=actor,
        authorize=self.authorize_conversation, write=True,
    )
    await actions.confirm(session, turn, confirmation, actor=actor)
    view = await actions.state(session, turn)
    await self._authorize_entry_result(session, actor, view)
    return view
```

函数虽然在 `async with` 内写了 `return`，但调用方取得返回值之前，仍会退出上下文并完成提交。提交失败时不会正常返回这个 view。因此 `confirm_chat()` 不会在确认事务尚未提交时就继续启动调查。

### 3.1 model_validate_json：检查确认数据的格式

```text
confirmation 对象
  → model_dump_json()：序列化为 JSON 文本
  → EntryConfirmation.model_validate_json()：重新校验、构造新对象
```

它检查 ID 格式、摘要格式、严格布尔值与额外字段约束。HTTP 已经做过请求体验证；业务入口这里再次规范化，覆盖直接调用服务方法的情况。

它不访问数据库，因此不能检查“调用是否存在”“摘要是否与原建议一致”或“用户是否有权确认”。格式合法、数据来源真实和业务允许是三件事。

### 3.2 owned_turn：检查归属、权限，并锁住要修改的 turn

代码：[owned_turn()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:17)。

```python
query = select(ChatTurnRow).where(ChatTurnRow.turn_id == turn_id)
if write:
    query = query.with_for_update()
turn = await session.scalar(query)
```

`write=True` 会请求数据库行锁。之后检查：

1. turn 是否存在。
2. 会话是否存在，`conversation.owner_id` 和 `turn.actor_id` 是否都等于当前 actor。
3. 当前 actor 是否具有 `chat:read`；写入还要具有 `chat:send`。
4. turn 是否属于 `ENTRY_DECISION` 协议。

返回的是数据库中的 `ChatTurnRow`，不是模型建议，也不是最终响应。

`FOR UPDATE` 保护并发修改顺序，不能代替用户权限。锁保持到当前事务提交或回滚，并不会保持到整个调查结束。

### 3.3 actions.confirm：检查“现在确认的是不是原建议”

代码：[actions.confirm()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:115)。

先按 `turn.turn_id` 锁定 `EntryDecisionRow`，再由 `read_decision()` 核验原请求摘要、决定摘要及调用、案件关联。这里的摘要核验用于一致性检查，不是数字签名，也不替代数据库写权限控制。

关键条件如下：

```python
if (
    not isinstance(value, ToolInvocation)
    or value.name != "start_investigation"
    or confirmation.invocation_id != row.invocation_id
    or confirmation.decision_digest != row.decision_digest
):
    raise chat.ChatConflictError("确认对象不属于已保存的启动请求")
```

假设原记录为“调用 A，启动 credit-v3”：普通回答不能拿来确认执行；进度查询不能冒充启动建议；调用 B 的编号不能确认调用 A；旧摘要不能确认另一份决定。

已有确认事实时：相同用户提交相同结果可以复用；将 true 改成 false，或换确认人，则拒绝。未确认时必须处于 `PENDING_CONFIRMATION`。

```python
row.confirmed = confirmation.confirmed
row.confirmation_actor = actor
row.confirmed_at = datetime.now(timezone.utc)
row.status = "READY" if confirmation.confirmed else "CANCELLED"
```

取消还会在同一事务中保存取消响应，使 turn 结算。确认则保留后续执行空间：动作变为 READY，turn 暂时仍为 STARTED。

```text
model_validate_json：数据结构合法吗？
owned_turn：这条请求属于你，而且你当前能操作吗？
actions.confirm：你确认的是数据库里原来的启动建议吗？
```

## 4. execute_entry：从可靠建议切换到真实业务动作

代码：[execute_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:216)。它接收 `turn_id` 和 actor，不接收一份新的模型决定。

### 4.1 执行前短事务：读取决定、鉴权、领取

```text
owned_turn(write=True)
  → 锁定并核验 turn
  → 锁定 EntryDecisionRow
  → read_decision()：核验保存的原决定
  ├─ 已 STARTED／已终态：返回已有视图
  └─ 必须为 READY 且是 ToolInvocation
       → 核对业务权限
       → actions.start()：READY → STARTED
       → 提交事务
```

业务权限分支不同：查询要求案件 READ；启动要求指定模型资源上的 create、READ、CONTINUE。`actions.start()` 还再次要求启动动作具有 `confirmed is True`，不能靠调用方直接把 READY 当作确认事实。

`row, claimed` 中的 `claimed` 表示本次是否成功领取动作，不表示工具已经成功。领取前后的数据库状态转换，防止两个并发请求都认为自己应该执行。

第一段事务结束后提取的 `case_id` 是此前已保存、稳定预分配的案件编号。预分配编号不等于案件表里已经有该案件。

### 4.2 事务外执行：两个工具走不同分支

查询已有案件：

```python
answer = await self._progress(actor, case_id)
result_case = case_id
```

启动新调查：

```python
source = InitialPlanningInput(
    user_query=UserQuery(
        query=message,
        model_id=decision.arguments.model_id,
    ),
    available_capabilities=["KNOWLEDGE_RETRIEVAL"],
)
result_case = await self._create_case(actor, case_id, source)
await self._run_case(actor, case_id)
answer = await self._progress(actor, case_id)
```

`message` 读取自保存的原请求；目标模型来自保存的原决定，不重新询问模型，也不从确认请求替换。

### 4.3 为什么还需要 _create_case 和 _run_case

因为确认只改变了聊天动作状态，并未建立调查事实。

| 步骤 | 建立或读取的事实 |
|---|---|
| 确认 READY | 用户同意了哪条已保存建议 |
| `_create_case()` | 案件原始调查输入、创建者、第一轮 |
| `_run_case()` | 取得可信运行上下文并调用原 Runner |
| `_progress()` | 读取最新一轮进度，形成用户可读文字 |

聊天与告警都需要后两种调查职责，所以复用这两个方法。聊天分支不会先调用一次 `from_alert()`；不存在“告警函数又执行一遍聊天函数”的嵌套关系。

### 4.4 结算短事务：把业务效果与聊天响应关联起来

执行成功时构造 `ChatResponse(status="SUCCEEDED", answer=..., investigation_id=result_case)`。已知错误构造 FAILED；未知依赖错误通常将动作标为 UNCERTAIN，同时内层 ChatResponse 为 FAILED。

最后重新检查会话写权限，调用 `actions.finish()`。它检查原调用必须 STARTED，turn、conversation、案件关联不能变化，再在一个事务中保存动作终态和聊天响应。

整个方法没有一个包住所有操作的大事务：聊天领取、案件创建、Runner 阶段入账、聊天结算分别提交。故障恢复必须依据已经提交的事实，不能把后面的失败理解成“前面全部回滚”。

## 5. _create_case：建立稳定案件，而不是每次生成一个新案件

代码：[_create_case()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:52)。

它先规范化 `InitialPlanningInput`，取得目标模型，并按 `case_id + 1` 生成稳定的第一轮 ID。检查创建权限后，在短事务中：

```text
对稳定案件键加事务锁
  ├─ 案件存在：核对原始调查输入，不覆盖 created_by
  └─ 案件不存在：保存 InvestigationRecord
  → 登记／复用同一第一轮 RoundRecord
  → 提交，返回 case_id
```

返回值只是案件 ID，所以 `result_case = await self._create_case(...)` 不是接收调查结论。这里也没有生成 AcceptedPlan；正式计划在图内准备和接受。

同一个案件 ID 对应不同输入时会冲突。稳定 ID 让重复请求可定位同一对象，但完整幂等还依赖输入比较、状态、唯一约束、阶段账本及执行器本身的副作用控制。

## 6. _run_case：Runner.ainvoke 的直接调用方

代码：[_run_case()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:89)。

```python
async def _run_case(self, actor, case_id):
    runtime = await self._runtime(actor, case_id, execute=True)
    return await self.runner.ainvoke(
        context=InvestigationInvocation(runtime=runtime)
    )
```

`_runtime()` 在一个读取会话中调用 `_load_runtime()`：加载数据库案件和第一轮记录，调用 `tools_for(actor, planning_input)` 取得当前应用授予的工具绑定，并检查 CONTINUE 权限。

这里从第一轮定位整条调查的稳定根入口。跨轮执行后也不简单改成“找最新轮次重新启动一个 Runner”，否则可能绕开原检查点定位与推进关联。显示最新进度是 `_progress()` 的另一项职责。

`InvestigationInvocation(runtime=runtime)` 是传递可信信息的 Python 对象，不是发给模型的 Prompt。调用方此时不提供内部 execution claim，Runner 必须自行鉴权和获取运行权。

## 7. from_alert：不同输入，同一个调查执行入口

代码：[from_alert()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:93)。

```python
actor = TypeAdapter(Identity).validate_python(actor)
request = AlertRequest.model_validate_json(request.model_dump_json())
source = InitialPlanningInput(
    monitoring_alert=request,
    available_capabilities=["KNOWLEDGE_RETRIEVAL"],
)
case_id = chat_store.stable_id("case-alert-", request.alert_id)
await self._create_case(actor, case_id, source)
result = await self._run_case(actor, case_id)
return {"investigation_id": case_id, **result}
```

| 对比点 | 聊天启动 | 告警触发 |
|---|---|---|
| 原始依据 | 保存的用户消息、模型目标 | 监测服务告警 |
| 稳定案件 ID | 基于聊天 turn | 基于 alert_id |
| 入口前置条件 | 原决定已确认并过执行权限检查 | API 身份及案件创建、运行权限检查 |
| 调查执行 | `_create_case()` → `_run_case()` | 同左 |
| 返回组织 | `_progress()` → ChatResponse → EntryActionState | 案件 ID 加 Runner 返回结果 |

`TypeAdapter(Identity)` 只检查 actor 格式，不是重新验证 Token；告警模型校验也不证明监测事件真实，更不会重新计算 PSI。当前入口将其作为监测服务提供的异常输入。

## 8. “响应中”是什么意思：Python 返回值如何到调用方

“HTTP 响应”是服务器通过网络返回给前端或监测程序的数据。业务方法先返回 Python 对象，路由再把它转换成 JSONResponse。

```python
# 字典展开语法示意。
case_id = "case-001"
result = {"run_id": "run-001", "round_complete": True}
body = {"investigation_id": case_id, **result}
```

`body` 等于：

```json
{
  "investigation_id": "case-001",
  "run_id": "run-001",
  "round_complete": true
}
```

这里的 `**result` 是把字典键值展开到另一个字典，不是再次运行 Runner。若键重复，后写入的值会覆盖先写入的值；当前 Runner 返回内容不包含 investigation_id。

告警路由对这个结果执行 `jsonable_encoder()` 后放入 JSONResponse。聊天路径没有直接返回 Runner 的完整字典，而是保存进度说明到 ChatResponse，再返回动作视图。因此“Python 方法返回值”“数据库响应记录”“HTTP 响应体”相关但不是同一个对象。

## 9. _progress：读取最新一轮的摘要，不运行调查

代码：[_progress()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:102)。

```text
检查案件 READ
  → 找到 round_number 最大的轮次，检查其 READ
  → 读取这轮 AcceptedPlanRow 和 InvestigationRunRow
  → 关联 InvestigationRunSelectionRow 与 ResultRow
  → 读取已选定产物的任务状态、Observation 状态
  → 读取 RoundProgressionRow 的停止原因
  → 拼接字符串 answer
```

为什么读取 selection？同一阶段可能有多个尝试，进度应采用本次运行正式选定的结果，不能随便拿数据库中任意一次尝试。

它不调用模型，不调用工具，不推断根因，也不返回所有历史发现。假设最新轮计划尚未接受，显示“尚未接受”；没有已提交结果则显示“暂无已提交结果”，不能据此判断从未开始。

当前文本固定说明案件仍为 OPEN。调查执行结束不等于案件关闭；任务执行成功也不等于根因已确认。失败、拒绝、零命中和解释不确定应结合各自记录判断，不能只看接口是否返回 200。

## 10. Runner.ainvoke：先定位运行，再决定首次执行还是恢复

代码：[InvestigationRunner.ainvoke()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/runner.py:44)。

```text
校验调用上下文和恢复策略
  → 鉴权、查已有规划和运行身份
  → _claim：取得运行权
  → aget_state：读取检查点
  → 解析当前轮次并与账本核验
  ├─ 已结束：复用输出
  └─ 首次／中断：调用图
  → 再次对账、返回类型化结果
  → 退出 claim，释放锁连接
```

函数参数全部为关键字参数：`context` 是可信调用包装；可选 `plan_id` 指定已有正式计划；`policy` 控制失败、不确定尝试是否允许重试，以及有歧义时选哪份已提交产物。

这里不接受客户端任意传入 thread_id 或 State 以跳过节点。恢复策略默认不允许自动重试失败或不确定尝试；HTTP 请求重发也不会自动切换为更宽松的重试策略。

### 10.1 几个变量的实际类型容易混淆

| 变量 | 在 Runner 中装什么 |
|---|---|
| 入参 `context` | 起初是 `InvestigationInvocation` |
| `context = require_invocation(context).runtime` 后 | `InvestigationRuntimeContext`，变量名被复用 |
| `run_identity` | 案件、入口轮次、运行、预留／正式计划 ID |
| `_claim() as invocation` | 内部 `RunExecutionContext`，不是外部请求包装 |
| `root` | 原 Runtime 加内部执行控制的 `InvestigationInvocation` |
| `active` | 核验正式后继关系后，当前轮次对应的调用包装 |

`require_invocation()` 拒绝外部预填 execution claim；Runner 后面自己取得锁后，才创建带 execution 的内部包装。变量名叫 context 不代表它是模型上下文。

### 10.2 鉴权发生在读取检查点之前

```python
trusted = await authorize_run(session, context)
```

[`authorize_run()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/access.py:247) 对数据库中的规范案件、轮次归属进行核对，并分别要求 CONTINUE 与 READ。拥有 READ 不自动拥有执行权，拥有执行权也不能据此跳过历史读取权限。

这不是让用户再次点击确认。确认固定用户对某个建议的选择，鉴权检查当前仍允许执行，二者保护不同事实。外部调用等待、恢复和切轮期间也不能只信任最初检查过的权限。

## 11. preparation：已经登记规划，不等于已有正式计划

这是 Runner 中最值得单独看的分支：

```python
existing_run = await session.scalar(
    select(InvestigationRunRow).where(
        InvestigationRunRow.round_id == round_id
    )
)
preparation = await session.scalar(
    select(PlanningRunRow).where(PlanningRunRow.round_id == round_id)
)
```

两者都是数据库记录，不是“是否准备好”的布尔变量，也不是正在执行的 Python Task。

| 记录 | 主要含义 | 不能据此声称什么 |
|---|---|---|
| `PlanningRunRow`，这里命名为 preparation | 本轮规划身份、预留 plan_id、规划模型及输入协议已登记 | 不保证模型已返回、候选已提交或计划已接受 |
| `PlanningAttemptRow` | 某次规划尝试及 STARTED／COMMITTED／FAILED／UNCERTAIN 状态 | COMMITTED 仅指候选可靠保存 |
| `PlanningAcceptanceRow` | 已接受计划与候选尝试之间的正式来源关系 | 不是工具执行结果 |
| `AcceptedPlanRow` | 已正式接受、允许交给执行阶段的计划 | 不保证工具已经执行 |
| `InvestigationRunRow`，这里命名为 existing_run | 正式执行的稳定身份及当前代次 | 不保证图已经结束 |

表定义在 [planning_models.py](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/persistence/planning_models.py:10)。规划记录由图内准备逻辑登记，Runner 查询 preparation 本身不会调用模型。

### 11.1 已有身份时必须复用，而不是重新分配

```python
if existing_run is not None or preparation is not None:
    owner = existing_run if existing_run is not None else preparation
    if owner.investigation_id != case_id or (
        plan_id is not None and plan_id != owner.plan_id
    ):
        raise ledger.LedgerConflictError(...)
    plan_id, run_id = owner.plan_id, owner.run_id
```

这里的 owner 表示“提供既有运行身份的记录”，不是会话 owner，也不是权限授予者。已有正式运行优先；没有正式运行但有规划记录时，使用规划阶段早已分配的身份。

假设进程在“候选已提交、计划未接受”时退出。恢复时 preparation 存在，Runner 取回原 run_id 和预留 plan_id，图内规划日志可以继续关联原候选，而不是再分配一套 ID。

### 11.2 没有运行记录时，检查是否已经有 AcceptedPlan

```text
已有正式计划且只有一份 → 使用它
有多份正式计划         → 要求可信调用方明确指定 plan_id
没有正式计划           → 走图内规划，稳定预留身份
```

保留已有 AcceptedPlan 入口，是为了接续已有正式记录及其检查点定位；它不会绕开计划存在性和归属核验。

### 11.3 creating_plan 与 allow_planning 为什么不是同一个判断

```python
creating_plan = plan_id is None

async with _claim(
    ...,
    allow_planning=creating_plan or preparation is not None,
) as invocation:
    ...
```

| 情况 | creating_plan | allow_planning 的依据 |
|---|---|---|
| 首次进入，还没有规划记录或正式计划 | True | 允许图内生成计划 |
| 恢复已有 preparation，plan_id 只是预留身份 | False | preparation 存在，仍允许继续规划接受 |
| 直接接续已有正式计划，没有 preparation | False | 不需要允许缺计划，必须查到 AcceptedPlan |

第二种情况尤其容易误读：`plan_id` 已经非空，所以 creating_plan 是 False，但 AcceptedPlan 可能仍然不存在。如果只检查 creating_plan 就禁止缺计划，会阻断正常的规划恢复。

## 12. run_id、plan_id 与 thread_id：同一个值出现，不代表同一种身份

没有正式计划时，代码基于案件与入口轮次计算稳定摘要，生成 run_id 和预留 plan_id。直接接续已有计划但没有运行记录时，则基于案件、轮次、计划生成运行 ID。

当前部分 run_id 的字符串前缀叫 `round-`，不要只凭前缀把它当作业务 round_id；字段及数据库关联才决定含义。

```python
run_identity = RunIdentity(
    run_id=run_id,
    investigation_id=case_id,
    round_id=round_id,
    plan_id=plan_id,
)
config = {"configurable": {"thread_id": run_id}}
```

`thread_id` 是 LangGraph 检查点定位键，不是操作系统线程、聊天 conversation_id 或 turn_id。沿原案件根入口恢复，才能找到同一条流程的检查点。

## 13. _claim：锁、运行代次和事务分别保护什么

代码：[_claim()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/run_guard.py:81)。

它使用专用自动提交连接取得会话级 advisory lock：先锁定入口运行，再锁定整个案件。获取失败时直接拒绝并发接续，不是在 Python 内无限等待重试。

```text
运行锁：阻止同一个根运行被同时接续
案件锁：阻止另一个入口拿后继轮次身份并发推进同一案件
generation：阻止失去运行权的旧代次提交结果
阶段事务：让登记、接受或提交的关联原子生效
```

专用锁连接使用独立的 NullPool 引擎。模型或工具调用期间持续持有会话锁，但不因此持续持有账本长事务或行锁；阶段账本仍使用短事务。它不是持锁等用户确认：聊天确认已经在进入 Runner 之前完成。

`_claim()` 随后重新鉴权、检查计划归属。如果有规划记录且计划已存在，还要求正式接受来源存在，避免把一份没有可靠候选来源的计划当作恢复起点。

### 13.1 generation=0 不等于没有运行权

```python
generation = 0
if plan_row is not None:
    generation = await _bind_execution_run(...)
yield RunExecutionContext(identity, policy, guard)
```

没有正式计划时，可以已经持有运行锁，但尚未绑定正式执行代次。计划正式接受后再建立 Run 与 claim；这是“允许规划”和“允许执行”的分界。

已有正式计划时，`_bind_execution_run()` 登记本次 RunClaim，推进 current_generation。后续提交检查锁连接与数据库当前代次，失联旧执行者不能仅凭原内存值继续提交。

因此，重调 Runner 即使复用所有业务结果，仍可能重新鉴权、取得锁并写入 claim 审计代次。不能把“不重复执行工具”说成“整个请求没有任何数据库写入”。

### 13.2 运行控制不进入 State

内部 `RunExecutionContext` 包含 identity、policy、guard 和推进预算；连接、授权函数及锁控制不序列化到业务检查点。

当前默认上限是 3 轮、累计 24 个任务、300 秒推进时间预算。时间预算在接受轮次推进时检查，不是到 300 秒就强制终止正在运行的同步调用。单次模型／工具等待还有各自超时机制。

## 14. 读取检查点：values 与 next 是两个问题

```python
snapshot = await self._graph.aget_state(config)
```

| 属性 | 表达什么 |
|---|---|
| `snapshot.values` | 已保存的业务 State |
| `snapshot.next` | 接下来需要执行哪些节点 |

当前允许的节点为 prepare_plan、task_execution、observation。若旧检查点停在已删除的 replanning 节点，代码会拒绝并要求显式迁移，不会静默映射到新回边。

然后依次执行：

```python
root = InvestigationInvocation(runtime=context, execution=invocation)
active = await current_round_call(snapshot.values, root, sessions=self._sessions)
await validate_checkpoint(
    snapshot.values, active.execution,
    sessions=self._sessions, runtime=active.runtime,
)
```

[`current_round_call()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/planning.py:181) 根据不可变后继记录，从根运行找到当前轮次，并重新核对授权。它不能直接接受 State 自称的 run_id 作为权限依据。

[`validate_checkpoint()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/checkpoints.py:69) 核对原始调查输入、当前身份、正式计划、结果、选定产物、Observation 及结束状态。新 v3 检查点结束时还必须与正式停止决定一致。

检查点保存流程位置与业务 State，账本保存正式执行事实。两者需要对账，不能因为检查点里有一条结果，就认定数据库一定已经接受了它。

## 15. 首次执行、接续、结果复用：三个分支

已经结束时：

```python
if snapshot.values and not snapshot.next:
    if not snapshot.values.get("round_complete"):
        raise ledger.LedgerIntegrityError(...)
    output = snapshot.values
```

有 State、没有待执行节点，且核验了结束事实，才直接复用。缺少结束标记时拒绝，不用“没有 next”掩盖损坏状态。

其他情况：

```python
initial = None if snapshot.values else to_checkpoint_data({
    "checkpoint_schema_version": 3,
    "run_id": run_id,
    "plan_id": plan_id,
    "planning_input": context.investigation.planning_input,
})
```

首次运行没有检查点，提供初始 State；已有检查点时传入 None 接续保存状态，不是清空原状态或重新发送原始输入覆盖它。

```python
output = await self._graph.ainvoke(
    initial,
    config,
    context=InvestigationInvocation(runtime=context, execution=invocation),
    durability="sync",
)
```

这是实际调用 LangGraph 的位置。`durability="sync"` 让检查点持久化与后续步骤衔接，但不把远端工具效果和 PostgreSQL 事务变成一个原子操作。

正式图仍为：

```text
prepare_plan → task_execution → observation
      ↑                             │
      └─ 接受推进、建立唯一后继轮次 ─┤
                                    └─ STOP／预算耗尽 → END
```

工具结果提交后、节点检查点更新前发生中断时，恢复可能重新进入节点。节点必须通过阶段账本复用已提交产物，不能仅依赖“这个节点以前跑过”。

## 16. 跨轮时什么保持稳定，什么必须变化

```text
根检查点 thread_id：保持稳定
案件运行锁：持续持有

第一轮：round-1、run-1、plan-1、结果及 Observation-1
   ↓ 正式推进关系，唯一后继
第二轮：round-2、run-2、plan-2、结果及 Observation-2
```

根 Runtime 仍作为整个调用的可信来源。节点通过 current_round_call 解析本轮身份，后继轮次拥有自己的正式计划和阶段账本，不能原地修改上一轮 AcceptedPlan 或把新结果登记到旧轮。

跨轮预算、后继关系、前一轮已提交 Observation 由程序检查。模型建议 CONTINUE 不直接赋予下一轮运行权，也不需要每轮重新让用户点击一次聊天启动确认。

Runner 结束前再次核验输出。返回的 run_id 与 plan_id 对应最后一轮；跨轮时另返回 root_run_id，表示稳定根定位。最新一轮执行结果不等于整案所有历史结果，历史仍按正式关联保存在账本中。

## 17. 三层状态：成功响应、调查停止、根因确认不能混用

| 层次 | 示例 | 说明 |
|---|---|---|
| 聊天动作 | READY／STARTED／SUCCEEDED／UNCERTAIN | 入口动作是否被领取、是否已可靠结算 |
| 调查推进 | round_complete、stop_reason、OPEN | 本次调查循环是否停止，以及为何停止 |
| 证据解释 | SUCCEEDED／INCONCLUSIVE，findings、未解决问题 | 是否可靠完成解释，有哪些依据与限制 |

例如 Runner 因轮次预算耗尽正常返回，聊天请求可能成功结算，但 stop_reason 仍说明调查未解决；不能把 HTTP 200 或 ChatResponse.SUCCEEDED 当作根因已确认。

返回字段的当前含义：

| 字段 | 内容 |
|---|---|
| `run_id`、`plan_id` | 最后一轮的身份配对 |
| `root_run_id` | 发生跨轮时额外返回的根运行定位 |
| `planning_input` | 原始调查输入 |
| `investigation_plan` | 最后一轮计划及任务状态 |
| `task_execution_results` | 最后一轮执行结果 |
| `observation` | 最后一轮正式解释 |
| `stop_reason` | 有记录时返回停止原因 |
| `round_complete=True`、`investigation_status="OPEN"` | 本次推进已结束，案件未因此关闭 |

## 18. 失败与恢复：在哪个提交边界中断最重要

| 中断位置 | 可能已经提交的事实 | 当前语义 |
|---|---|---|
| 确认事务提交前 | 原待确认建议 | 确认未生效，事务内修改回滚 |
| 确认已提交、尚未领取动作 | READY、确认事实 | 原确认重发可重新核验并尝试执行 |
| STARTED 已提交、案件尚未创建 | 原动作与稳定案件关联 | 不能当作未开始自动重领 |
| 案件已创建、Runner 尚未完成 | 案件、第一轮，可能有部分阶段结果 | 后续失败不会回滚已提交的案件 |
| preparation 已登记、候选未可靠返回 | 规划身份、尝试 STARTED | 按原日志核实，不随意换 ID |
| 候选已提交、正式接受前 | 候选与 task_id | 恢复可以复用候选；不是直接执行未接受计划 |
| 工具结果已提交、检查点未更新 | 正式阶段产物 | 重入节点时从账本复用 |
| 推进已提交、下一轮检查点未更新 | 唯一后继关系 | 复用后继，不再创建另一轮 |
| 图已结束、聊天结算前 | 调查结果可能完整，入口动作仍 STARTED | 查原 turn 关联案件，入口不会自动重跑 |
| 聊天结算已提交、HTTP 响应丢失 | 最终动作和响应 | 原请求／确认重试读取已有结果 |

Runner 首次与恢复共用一个方法，但不会自动接管所有入口层故障。聊天 execute_entry 看到 STARTED 或 UNCERTAIN 会返回已有状态；它没有因此自动重新调用 Runner。直接再次调用受控告警入口或受控 Runner 时，也仍必须经过身份、锁、账本与恢复策略检查。

同一个底层异常在两种入口可能被不同层捕获：告警路由通过 `invoke()` 映射异常；聊天 execute_entry 会将执行阶段异常组织为失败／不确定响应。不能仅凭一个 HTTP 状态码就断言案件表没有写入。

退出 `_claim()` 的 finally 会清理专用锁连接，包括异常路径。连接失效、进程退出或运行权丢失时，后续提交仍要通过 guard 检查。释放锁不会删除已提交的业务记录，也不替代外部工具的幂等保障。

## 19. 最后用一次确认串起代码跳转

| 顺序 | 代码入口 | 带着什么问题阅读 |
|---|---|---|
| 1 | [HTTP confirm_chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:77) | URL、确认体和认证身份分别从哪来？ |
| 2 | [EntryServices.confirm_chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:137) | 哪个返回状态才进入执行？ |
| 3 | [confirm_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:204) | 格式、归属和确认事实在哪个事务检查？ |
| 4 | [owned_turn()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:17) | 行锁保护什么，权限由谁判断？ |
| 5 | [actions.confirm()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:115) | 调用编号、摘要和已确认事实怎样比较？ |
| 6 | [execute_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:216) | STARTED 何时提交，真实执行从哪行开始？ |
| 7 | [_create_case()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:52) | 同一个稳定案件何时创建，重复输入怎样核对？ |
| 8 | [_run_case()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:89) | Runner 的直接调用方是谁？ |
| 9 | [Runner.ainvoke()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/runner.py:44) | preparation、已有计划和已有运行分别如何处理？ |
| 10 | [_claim()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/run_guard.py:81) | 案件锁和代次为何不能由 State 提供？ |
| 11 | [current_round_call()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/planning.py:181) | 根检查点如何可信地解析到下一轮？ |
| 12 | [validate_checkpoint()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/checkpoints.py:69) | 流程位置和正式事实是否一致？ |
| 13 | [_progress()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:102) | 为什么聊天返回的是进度，而不是 Runner 全部字典？ |
| 14 | [actions.finish()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:154) | 最终动作和 ChatResponse 如何一起结算？ |

范围说明：本文解释现有代码，没有实施新的迁移、部署或恢复操作，没有调用真实模型。调查阶段的生成、接受和执行机制可继续沿 planning.py、phase_execution.py 阅读；本文不把接口可恢复、检查点存在或调用成功等同于所有外部副作用都能自动恢复。


`InvestigationRunner.ainvoke()` 是**调查图的统一执行入口**：先确定“运行哪份调查、是否有权运行、从哪里继续”，再调用 LangGraph，最后核对并返回结果。

入口：[runner.py:44](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/runner.py:44)。

它本身不直接生成计划或执行检索，那些工作由图节点完成。

```text
校验调用上下文与恢复策略
    ↓
鉴权，查找已有规划／运行身份
    ↓
取得运行锁，建立本次执行控制
    ↓
读取并核验检查点
    ├─ 已结束：复用结果
    ├─ 未开始：提交初始 State，运行图
    └─ 已中断：从检查点继续
    ↓
再次与账本对账，返回结果
    ↓
释放运行锁
```

**① 函数参数分别是什么**

```python
async def ainvoke(
    self,
    *,
    context: InvestigationInvocation,
    plan_id=None,
    policy: RecoveryPolicy | None = None,
):
```

| 参数 | 含义 |
|---|---|
| `context` | 可信身份、案件、入口轮次、授权函数、工具绑定 |
| `plan_id` | 可选，明确指定从哪份已接受计划开始 |
| `policy` | 是否允许重试失败／不确定尝试，以及选择哪份历史产物 |

`*` 表示这些参数都必须用关键字传递。

`ainvoke` 是异步方法名，不是 Python 关键字。调用方通过 `await runner.ainvoke(...)` 等待调查执行或恢复完成。

**② 校验上下文，重新检查权限**

```python
context = require_invocation(context).runtime

policy = RecoveryPolicy.model_validate_json(
    (policy or RecoveryPolicy()).model_dump_json()
)
```

第一行从调用包装中取得 Runtime，同时拒绝调用方预先塞入内部执行 claim。**运行权必须由 Runner 自己取得。**

第二行校验恢复策略。默认策略不允许盲目重试不确定尝试。

接下来：

```python
trusted = await authorize_run(session, context)
```

它在读取检查点之前检查数据库中的正式归属，以及当前用户的 **READ 和 CONTINUE 权限**。知道一个 `run_id`，不代表有权读取或接续它。

**③ 查找已有身份，避免每次调用都创建新运行**

```python
existing_run = ...  # 本轮已经建立的正式运行
preparation = ...   # 本轮已经登记的规划运行
```

这里分三种情况：

| 数据库情况 | 行为 |
|---|---|
| 已有运行或规划记录 | 使用原 `run_id`、`plan_id`；指定了冲突计划则拒绝 |
| 尚无运行，但已有正式计划 | 使用该计划；多份计划时要求明确指定 |
| 尚无正式计划 | 根据案件和轮次稳定生成运行 ID、预留计划 ID |

**预留 `plan_id` 不等于计划已经接受。** 后续仍必须在 `prepare_plan` 中完成候选生成、校验和正式接受。

随后构造：

```python
run_identity = RunIdentity(
    run_id=run_id,
    investigation_id=case_id,
    round_id=round_id,
    plan_id=plan_id,
)

config = {"configurable": {"thread_id": run_id}}
```

`RunIdentity` 表达业务记录归属；`thread_id` 用于定位 LangGraph 检查点。这里的 `thread_id` **不是操作系统线程编号**。

**④ `_claim()`：取得这次推进的运行权**

```python
async with _claim(
    self._lock_engine,
    self._sessions,
    run_identity,
    context,
    policy,
    allow_planning=creating_plan or preparation is not None,
) as invocation:
```

[_claim()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/run_guard.py) 会：

- 取得运行锁和案件锁，阻止另一请求并发推进同一调查。
- 重新核对权限及计划归属。
- 已有正式计划时，建立本次运行代次 `generation`。
- 返回内部 `RunExecutionContext`：身份、策略、锁检查器和预算。

可以这样理解：

```text
runtime：谁在操作、允许操作什么？
invocation：本次是否持有运行权、提交属于哪个代次？
```

没有正式计划时，`generation=0` 表示尚未绑定正式执行代次，**不表示没有取得锁**。正式接受计划后才建立执行代次。

这期间持续持有专用连接上的会话锁，但不是一直开启一个长账本事务。外部模型调用仍发生在短事务之外。

**⑤ 读取检查点，核验它能否继续使用**

```python
snapshot = await self._graph.aget_state(config)
```

主要看两部分：

| 字段 | 含义 |
|---|---|
| `snapshot.values` | 已保存的业务 State |
| `snapshot.next` | 接下来待执行的节点 |

代码先检查待执行节点是否属于当前拓扑：

```python
{"prepare_plan", "task_execution", "observation"}
```

例如旧检查点仍停在已删除的 `replanning` 节点，会明确拒绝，不能悄悄跳过。

然后：

```python
active = await current_round_call(
    snapshot.values, root, sessions=self._sessions
)

await validate_checkpoint(
    snapshot.values,
    active.execution,
    sessions=self._sessions,
    runtime=active.runtime,
)
```

这两步分别负责：

- [`current_round_call()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/planning.py:181)：沿数据库中的正式后继关系，确定检查点目前处于哪一轮，不能只相信 State 写的运行 ID。
- [`validate_checkpoint()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/workflow/checkpoints.py:69)：核对正式计划、执行结果、Observation 等是否与账本一致。

**⑥ 判断直接返回，还是调用图**

已经结束：

```python
if snapshot.values and not snapshot.next:
    if not snapshot.values.get("round_complete"):
        raise ...
    output = snapshot.values
```

有数据、没有待执行节点，并且结束标记合法，就复用结果，不重新运行模型或工具。

否则：

```python
initial = None if snapshot.values else to_checkpoint_data({
    "checkpoint_schema_version": 3,
    "run_id": run_id,
    "plan_id": plan_id,
    "planning_input": context.investigation.planning_input,
})
```

| 情况 | `initial` |
|---|---|
| 首次运行，没有检查点数据 | 初始 State |
| 已有检查点，需要接续 | `None`，沿用保存的 State |

真正运行 LangGraph 的位置是：

```python
output = await self._graph.ainvoke(
    initial,
    config,
    context=InvestigationInvocation(
        runtime=context,
        execution=invocation,
    ),
    durability="sync",
)
```

这里要区分两个同名方法：

```text
InvestigationRunner.ainvoke()
    管身份、运行权、检查点核验和结果返回
        ↓
self._graph.ainvoke()
    按 LangGraph 拓扑执行节点
```

`durability="sync"` 要求同步持久化检查点后再进入后续步骤，但它**不能单独保证工具不重复执行**。阶段账本和幂等控制仍由 `_phase()` 等代码负责。

**⑦ 跨轮时，检查点位置稳定，轮次身份独立**

例如：

```text
同一个根 thread_id
    第一轮：run-1 / plan-1
        ↓ Observation 建立正式后继关系
    第二轮：run-2 / plan-2
```

图仍在同一次调用中继续。每个节点通过 `current_round_call()` 取得当前轮身份，并复用持续持有的案件运行权，不会把第二轮结果写到第一轮账本。

**⑧ 最后对账、返回，并释放锁**

图结束后，再解析当前轮次、核验输出，然后返回：

| 返回字段 | 含义 |
|---|---|
| `run_id`、`plan_id` | 最后一轮对应的运行与计划 |
| `planning_input` | 原始调查输入 |
| `investigation_plan` | 最后一轮计划及任务状态 |
| `task_execution_results` | 最后一轮执行结果 |
| `observation` | 最后一轮解释 |
| `stop_reason` | 已保存的停止原因，如有 |
| `root_run_id` | 跨轮时额外返回根运行定位 |
| `investigation_status` | 当前仍为 `OPEN` |

返回的不是所有轮次的完整报告，历史事实仍保留在账本中。

无论正常返回还是抛异常，退出 `_claim()` 都会清理专用锁连接。**失败不等于数据库回到运行前：已提交的计划和阶段结果仍然存在，下次能否复用或重试，要根据检查点、账本和恢复策略核实。**

还要区分三种检查：
- authenticate()：请求者是谁。
- authorize_run()：当前是否有权读取、继续这个调查，且案件与轮次归属是否正确。
- guard.check()：本次执行者是否仍持有运行权，代次是否有效。

意思是：**不同操作，检查不同的权限；不是所有地方都调用 `authorize_run()`。** 注释 1

比如用户 A：

| 操作 | 要检查的权限 | 当前检查入口 |
|---|---|---|
| 创建案件 | 能否创建这个模型的调查 | `_create_case()` 中的 `_resource_permission()` |
| 查询进度 | 能否读取这个案件 | `_load_runtime(execute=False)` 等读取检查 |
| 运行调查 | 能否读取并继续这个案件 | `authorize_run()` |
| 检索知识 | 能否读取这些范围的资料 | 知识工具自己的授权检查 |

为什么要分开？例如：

> A 可以查看调查进度，但不能继续执行调查。

查询时只检查 **READ**，应该允许。  
如果查询也调用 `authorize_run()`，它同时要求 **READ + CONTINUE**，就会把 A 的合法查询拒绝掉。

再例如：

> A 可以运行 credit-v3 的调查，但不能读取其他模型的内部文档。

`authorize_run()` 通过后，知识工具仍须检查资料范围，不能因此放开所有文档。

所以，`authorize_run()` 的具体意思是：**检查当前用户是否可以读取并推进这次调查。它不是检查系统所有权限的总函数。**
之前的两个位置只是帮助入门。完整来看，**鉴权要放在读取、执行、提交和切换调查对象这些边界之前。** 注释 1

以“用户 A 调查 credit-v3”为例：

| 时机 | 检查什么 | 不通过怎么办 |
|---|---|---|
| **创建案件前** | A 能否对 credit-v3 创建调查 | 不创建案件 |
| **读取案件、检查点前** | A 能否读取该案件 | 不返回数据 |
| **开始或恢复调查前** | A 是否有权继续调查 | 不运行调查图 |
| **接受候选计划前** | 等待模型后，A 是否仍有权限 | 不把候选变成正式计划 |
| **执行具体工具前** | 工具是否获准，参数是否在允许范围内 | 不调用工具 |
| **工具返回、提交结果前** | 权限是否仍有效 | 拒绝提交；不能撤回已经发生的工具操作 |
| **建立下一轮前** | 是否允许继续，并且新轮次归属正确 | 不建立下一轮 |
| **查询历史结果时** | 当前是否仍能读取这些数据 | 不因以前读过就放行 |

**这些检查不全由 `authorize_run()` 完成。**

```text
创建案件       → 创建权限检查
仅查询进度     → READ 检查
运行／恢复调查 → authorize_run：READ + CONTINUE
调用具体工具   → 工具自身的权限与范围检查
提交阶段结果   → 权限检查 + guard 运行权检查
```

还有一个区别：**“用户有权限”和“这个执行者有运行权”不同。**

A 可能有权调查，但另一请求已经在推进同一个案件。此时不是权限不足，而是运行锁阻止并发执行；旧执行者失去运行权后，也不能继续提交。

所以不需要机械地在每个函数前重复鉴权。要先问：**接下来要读取什么、执行什么或提交什么；之前的检查是否已经跨过了等待、事务或对象切换。**
**应在“读取受保护的数据”和“执行或提交业务动作”之前鉴权。** `authorize_run()` 不只检查用户，还核对传入 Runtime 是否与数据库中的案件、轮次一致。

在当前调查链路中，主要有这些位置：

| 时机 | 为什么检查 |
|---|---|
| Runner 读取运行记录、检查点之前 | 防止无权用户读取或接续调查 |
| 取得运行锁后、建立执行 claim 之前 | 等待取锁期间，权限或数据库事实可能变化 |
| 模型／工具调用前的登记事务中 | 确认当前仍允许推进 |
| 外部调用返回、提交结果之前 | 调用可能耗时，不能直接沿用调用前的权限和运行权 |
| 接受下一轮、切换轮次之前 | 核对新轮次归属及当前权限 |
| 恢复或返回已保存结果之前 | 历史数据存在，不代表当前用户仍有权读取 |

例如一个阶段：

```text
鉴权、核对运行权
    ↓
提交 STARTED
    ↓
事务外调用模型／工具
    ↓
再次鉴权、核对运行权
    ↓
提交结果
```

**但不是每调用一个内部函数，都要重新执行 `authorize_run()`。** 同一个短事务、同一份已核验数据，没有跨越等待或信任边界时，可以复用检查结果。重复调用必须对应具体风险，不能仅因为“更安全”就层层增加。

还要区分三种检查：

- `authenticate()`：请求者是谁。
- `authorize_run()`：当前是否有权读取、继续这个调查，且案件与轮次归属是否正确。
- `guard.check()`：本次执行者是否仍持有运行权，代次是否有效。

另外，`authorize_run()` **同时要求 READ 和 CONTINUE**，适用于运行、恢复调查。仅查询进度时应检查 READ，不能因为用户没有 CONTINUE 就拒绝其合法读取。

权限可能在检查之后立即变化，因此它也不是“检查一次就永久授权”。实际保证取决于授权来源与事务边界；当前实现是在关键读取、执行和提交边界重新核验。