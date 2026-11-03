# Workflow 恢复执行解析：运行锁、账本与三个事务

本文对应 `backend/src/risk_platform/workflow/recovery.py`，重点解释 `_claim()`、`_ClaimGuard.check()`、`_selection()` 和 `_phase()`。这是当前实现的阅读指南，不是新增功能设计。

## 1. 先理解要解决的问题

一次调查可能包含两个知识任务：

```text
已接受计划
  ├─ T0：生成检索 Query → 知识检索
  └─ T1：生成检索 Query → 知识检索
                  ↓
              Observation
```

假设 T0 已经完成，T1 调用模型时进程退出。重新运行时不能简单从头再做，也不能把所有未完成记录都认定为失败。

系统需要回答四个问题：

1. 这个逻辑操作是否已经有可靠保存的结果？
2. 上次是否已经开始尝试，但没有保存最终结果？
3. 当前调用方是否仍有权读取历史、继续执行？
4. 是否已经有另一个进程正在执行同一轮？

账本记录执行事实，运行锁保护本轮执行权，短事务保护数据库状态变化，LangGraph 负责节点推进。

## 2. 必须区分的对象

| 对象 | 含义 | 示例 |
|---|---|---|
| Investigation | 整个案件 | CASE-001 |
| Round | 案件中的一轮调查 | ROUND-001 |
| Accepted Plan | 本轮已正式接受的计划 | PLAN-001 |
| Run | 该案件、轮次、计划对应的稳定执行身份 | 由三个身份确定的 run_id |
| Operation | 一项稳定的逻辑工作 | T0 的 Query Formulation |
| Attempt | 对该逻辑工作的一次尝试 | 第一次调用、显式重试后的第二次调用 |
| Result / Artifact | 一次尝试可靠保存的阶段产物 | Query 请求、执行结果、Observation |
| Generation | 同一个 run 每次取得运行权后的代次 | 第一次为 1，恢复后为 2 |

同一个 run 可以先后经历多次恢复调用，但不应同时被两个调用执行。同一个 Operation 可以有多个 Attempt，旧尝试不会被新尝试覆盖。

## 3. 账本与检查点各管什么

**检查点（Checkpoint）**记录 LangGraph 的状态和推进位置，例如某个节点是否完成。

**账本（Ledger）**记录业务阶段的可靠事实，例如某个 Query 是否已经提交、某次检索是否有保存结果。

两者不是同一个事务一起保存的。因此可能发生：

```text
某个阶段的结果已经提交到账本
    ↓
节点还没完成、检查点还没保存
    ↓
进程退出
```

恢复时节点可能再次进入，但 `_phase()` 会从账本复用已提交结果，从而避免重新调用这个阶段的外部服务。检查点不能凭自己声称“已完成”就绕过账本核验。

## 4. 两种锁的区别

### 4.1 会话级运行锁：同一轮由谁执行

`_claim()` 使用专用数据库连接执行：

```python
connection = await engine.connect()
connection = await connection.execution_options(isolation_level="AUTOCOMMIT")

pid, acquired = (
    await connection.execute(
        text(
            "SELECT pg_backend_pid(), "
            "pg_try_advisory_lock(hashtextextended(:key,0))"
        ),
        {"key": f"riskmind:first-round:{spec.run_id}"},
    )
).one()

if not acquired:
    raise RecoveryBlockedError("同一轮正在运行，拒绝并发接续")
```

执行步骤：

1. 使用 `run_id` 构造字符串键。
2. `hashtextextended()` 把字符串转换为整数锁键。
3. `pg_try_advisory_lock()` 尝试获得对应的会话锁。
4. 成功返回 `True`；另一连接已持有时返回 `False`，不会一直排队等待。
5. `pg_backend_pid()` 返回当前持锁连接对应的 PostgreSQL 后端进程 ID。

这把锁不依赖某条表记录。它是应用约定使用的锁编号；绕过该协议直接操作数据库的代码，不会自动被它拦住。哈希键理论上也可能碰撞，导致无关运行互斥；不能将其理解为数学上绝无冲突的编号。

该锁跨越短事务和外部调用。事务提交不会释放它。`_claim()` 退出时通过废弃、关闭专用连接释放会话锁；进程退出后，数据库在检测到连接断开时也会释放锁，不保证任何网络故障都被瞬间发现。

### 4.2 行锁：保护当前事务中的数据修改

`_ClaimGuard.check()` 查询 run 时使用：

```python
select(InvestigationRunRow).where(
    InvestigationRunRow.run_id == self.run_id
).with_for_update().execution_options(populate_existing=True)
```

`with_for_update()` 对应 `SELECT ... FOR UPDATE`。它锁住对应 run 记录，保护当前事务内的检查与后续修改。事务提交或回滚后，行锁释放。

`populate_existing=True` 要求刷新 ORM 中已有对象，避免依赖会话里缓存的旧 generation。

| 对比项 | 行锁 | 当前运行锁 |
|---|---|---|
| 保护对象 | 某条数据库记录 | 应用按 run_id 约定的锁编号 |
| 生命周期 | 当前事务 | 持锁数据库会话 |
| 提交后 | 释放 | 仍然保留 |
| 等待外部 API 时 | 前一个事务已结束，不保留其行锁 | 继续保留 |
| 主要目的 | 避免关键记录修改冲突 | 避免同一轮被同时执行 |

### 4.3 Generation：旧运行不能回来提交

每次成功 claim 都会保存新的 RunClaim，并将 `current_generation` 增加 1。

```text
运行 A：generation = 1
    ↓ 持锁连接失效，但外部 API 可能还在返回途中
运行 B：取得锁，generation = 2
    ↓
运行 A 尝试保存结果
    ↓
锁检查或 generation 检查失败，拒绝旧运行提交
```

Guard 检查连接是否失效、后端 PID 是否变化、专用会话是否仍持有 advisory lock，以及数据库 generation 是否匹配。当前锁状态查询检查该专用会话是否持有已授予的 advisory lock；它依赖该连接仅用于本次运行锁的约定。

这不等于取消旧的外部请求。旧请求可能仍执行并计费，但不能因此被允许以过期身份提交结果。

## 5. 数据库事务到底是什么

这里可把事务理解成：**一组数据库修改，要么一起可靠提交，要么一起回滚。**

```python
async with sessions.begin() as session:
    # 当前事务中的数据库操作
    ...
```

在当前使用方式下：

- 正常退出上下文：提交事务；提交本身也可能失败。
- 异常退出：回滚当前事务。
- 事务中调用 `return`：先执行上下文退出、完成提交，再真正返回。
- `flush()`：把待写入内容发送给数据库，执行相应约束检查，但不代表事务提交成功。

例如：

```python
async with sessions.begin() as session:
    session.add(row)
    await session.flush()
    raise RuntimeError("模拟失败")
```

即使已经 `flush()`，异常仍会使本事务回滚。不能把 flush 后的记录当成已经可靠完成。

事务只覆盖该数据库事务中的工作，不能撤销已经执行的模型 API、已产生的费用或其他外部副作用。

## 6. `_phase()` 的输入与输出

```python
async def _phase(sessions, invocation, value, produce):
    ...
```

| 参数 | 作用 |
|---|---|
| sessions | 创建短数据库会话和事务 |
| invocation | 当前可信上下文、运行规格、恢复策略、运行权检查器 |
| value | 当前 Operation 的稳定业务输入 |
| produce | 实际完成阶段工作的异步函数 |

`produce` 可以是 Query 生成、知识检索或 Observation 生成。传递函数本身不会执行它：

```python
await _phase(sessions, invocation, operation_input, query)
```

直到 `_phase()` 执行下面这行，才真正调用：

```python
artifact = await produce()
```

若历史中已有可复用产物，就直接返回，不会执行 `produce()`。

## 7. 总时间线：三个事务不是同时打开

```text
外层取得运行锁
│
├─ 事务 1：鉴权、核对操作、查历史、选结果、结算旧 STARTED
│    └─ 提交，释放本事务行锁
│
├─ 如果有正式结果：直接返回，跳过后续调用
│
├─ 事务 2：鉴权、检查执行权、检查重试策略、保存新 STARTED
│    └─ 提交，释放本事务行锁
│
├─ await produce()
│    └─ 等待模型或检索；不持有上述事务，但运行锁连接仍然占用
│
├─ 事务 3：鉴权、检查执行权、保存 Result + COMMITTED + Selection
│    └─ 一起提交，释放本事务行锁
│
└─ 外层调用退出时释放运行锁
```

“外部调用期间没有长事务”不表示没有任何数据库连接占用。专用运行锁连接仍然保持打开，代码使用独立 `NullPool` 连接以避免长期占用业务会话池中的连接。

## 8. 事务 1：先判断有没有必要执行

### 8.1 重新鉴权并检查运行权

```python
await _authorize(session, invocation.context)
await invocation.guard.check(session)
```

读取历史和继续执行是两个独立授权。之前允许，不代表恢复时仍允许。Guard 再确认当前调用仍拥有运行权。

### 8.2 登记操作与读取历史

```python
key = await access.register_authorized_operation(
    session, value, context=invocation.context
)
history = await access.read_operation_history(
    session, value, context=invocation.context
)
```

`operation_key` 标识逻辑工作，输入摘要标识这项工作的业务输入。相同 key、相同输入可识别复用；相同 key、不同输入发生冲突，不能偷偷改变原操作。

“ID 一样”不能证明“输入一样”，因此需要同时检查稳定输入。

### 8.3 选择可靠保存的结果

```python
artifact = await _selection(session, invocation, value, history)
if artifact is not None:
    return artifact
```

`_selection()` 的规则：

1. 本轮已经固定选择：核验后复用，不允许换另一份结果。
2. 没有固定选择，但只有一份可用的已提交产物：固定并复用。
3. 存在多份已提交产物：必须显式选择，不能自动挑“最新成功”。
4. 没有已提交产物：返回 `None`，继续判断是否允许执行。
5. 验证检查点时使用 `require=True`：若缺少正式 Selection，直接报完整性错误。

这里的 `return` 仍会先退出事务上下文；如果本事务新写入了 Selection，它会先提交，然后返回。

### 8.4 结算旧 STARTED

没有可复用结果时，检查历史中的 STARTED。只有它绑定同一 run，并且 generation 小于当前代次，才允许自动结算为 UNCERTAIN。

```python
if (
    binding is None
    or binding.run_id != invocation.spec.run_id
    or binding.generation >= invocation.guard.generation
):
    raise RecoveryBlockedError(...)
```

这避免把不明来源的尝试，或当前/更新代次的尝试，随意认定为已中断。

`UNCERTAIN` 表示：没有可靠保存结果，且不知道外部调用是否已经发生或完成。它不等于确定失败。

### 8.5 为什么事务 1 必须独立提交

假设事务 1 把旧 STARTED 改为 UNCERTAIN，事务 2 因默认策略不允许重试而抛错。

如果合成一个事务，抛错会把 UNCERTAIN 的结算一起回滚，历史又变回 STARTED。

分开后：

```text
事务 1：确认旧尝试不确定 → 提交
事务 2：当前策略不允许重试 → 回滚并报错
```

“不确定”的事实会保留下来，不因“拒绝继续”而消失。

## 9. 事务 2：先登记，再调用

重新鉴权、检查 Guard、读取最新历史，然后检查策略：

```python
if any(fact.status == "UNCERTAIN" for fact in history or ()) \
        and not invocation.policy.retry_uncertain:
    raise RecoveryBlockedError(...)

if any(fact.status == "FAILED" for fact in history or ()) \
        and not invocation.policy.retry_failed_attempts:
    raise RecoveryBlockedError(...)
```

`history or ()` 表示历史为 `None` 或空时使用空元组，便于统一遍历。`any(...)` 表示是否至少有一条符合条件。

这些规则在“没有正式结果被复用”的路径上执行；已提交的业务失败产物也可能已经在事务 1 被复用，并不会因为叫 FAILED 就自动重试。

允许执行时：

```python
attempt_id = "attempt-" + uuid4().hex
await access.start_authorized_attempt(
    session, key, attempt_id,
    context=invocation.context,
    retry=bool(history),
)
session.add(InvestigationRunAttemptRow(
    attempt_id=attempt_id,
    run_id=invocation.spec.run_id,
    generation=invocation.guard.generation,
))
await session.flush()
```

新 Attempt 记录本次可信 actor 等归属信息，RunAttempt 关联 run 和 generation。`bool(history)` 只表示是否存在历史；它不是绕过前面策略检查的自动重试开关。

退出事务 2 后，STARTED 才可靠提交。此后即使进程退出，数据库也保留这次尝试已经登记的事实。

## 10. 外部调用为什么放在事务之外

```python
artifact = await produce()
```

这是实际执行点。调用可能等待网络、模型或向量数据库。

若合并成一个大事务：

```text
BEGIN
保存 STARTED，未提交
调用模型成功，已计费
进程退出
数据库回滚，STARTED 消失
```

系统看不到上次尝试，却无法撤销模型调用。大事务既不能解决跨系统原子性，还会长时间持有事务和锁。

当前做法先提交 STARTED，再调用外部服务。这样能够识别“不确定尝试”，但仍不能证明请求一定已经发送：进程也可能在提交 STARTED 后、真正发出请求前退出。

如果 `produce()` 抛错或任务取消，这里不统一伪造 Attempt.FAILED；STARTED 会留下，由后续恢复结合归属判断处理。若阶段逻辑已经把一个已知业务失败转换为合法 Artifact 返回，则可以进入事务 3 正式保存该失败事实。

## 11. 事务 3：结果与提交状态不能分家

```python
result = await ledger.commit_result(
    session, attempt_id, "result-" + uuid4().hex, artifact
)
session.add(InvestigationRunSelectionRow(
    run_id=invocation.spec.run_id,
    operation_key=key,
    result_id=result.result_id,
    generation=invocation.guard.generation,
))
await session.flush()
```

进入该事务时再次鉴权、检查 Guard，因为等待外部服务期间权限、连接和代次可能已经发生变化。

同一个事务提交三件事：

1. 保存 Result。
2. 将 Attempt 更新为 COMMITTED。
3. 保存本轮正式采用该 Result 的 Selection。

不能允许“标记 COMMITTED 却没有结果”，也不能让本轮正式选择指向没有可靠提交的产物。任一写入、检查或最终提交失败，当前事务整体回滚。

但事务 2 已经独立提交，所以事务 3 回滚后，先前的 STARTED 不会一起消失。内存中拿到了 Artifact，也不等于数据库已经保存结果。

**COMMITTED 只表示这次尝试的结果可靠落库。** 其中 `TaskExecutionResult.status` 可以是 SUCCEEDED、FAILED 或 REJECTED；Observation 也可能是 INCONCLUSIVE。不要把持久化状态当成业务成功状态。

## 12. 崩溃位置决定恢复行为

| 中断位置 | 可靠保存的事实 | 后续处理 |
|---|---|---|
| 事务 2 提交前 | 本次新 STARTED 尚未可靠提交 | 本次 produce 尚未被调用；恢复重新检查历史 |
| STARTED 提交后、produce 前 | STARTED | 无法仅凭记录证明未调用，旧代次尝试按规则结算 UNCERTAIN |
| produce 执行中 | STARTED | 外部结果未知，默认阻止自动重试 |
| produce 返回后、事务 3 提交前 | STARTED | 返回值尚未可靠落库，仍可能需要结算 UNCERTAIN |
| 事务 3 提交成功、节点检查点保存前 | Result + COMMITTED + Selection | 再次进入阶段时复用产物 |
| 检查点也已可靠保存 | 阶段结果和图状态均可核验 | 按图恢复，并校验账本一致性 |

若客户端收到提交异常，也不能一概认定服务端已经回滚：数据库可能已经提交但确认响应丢失。恢复应重新读取可靠历史，不靠调用方猜测。

## 13. 完整例子：T0 不重做，T1 显式重试

第一次运行 generation=1：

```text
T0 Query       → COMMITTED
T0 Execution   → COMMITTED，execution_id=E0
T1 Query       → COMMITTED
T1 Execution   → STARTED
进程退出
```

恢复运行 generation=2：

```text
T0 Query       → 复用
T0 Execution   → 复用 E0
T1 Query       → 复用
T1 Execution   → 旧 Attempt 结算 UNCERTAIN，并提交
               → 默认策略拒绝再次调用
```

这次恢复停止，但旧尝试的 UNCERTAIN 已可靠保存。

可信调用方明确接受风险，并在当前授权通过后使用：

```python
RecoveryPolicy(retry_uncertain=True)
```

下一次恢复创建新的 Attempt，重新执行 T1。旧 Attempt 保持 UNCERTAIN，不覆盖；T0 的 E0 保持不变。

“允许重试”不等于已经确认上次没有执行。它只是明确接受可能重复调用、重复计费的风险。当前没有自动向外部服务查询执行状态或实现所有工具通用的恰好一次协议。

## 14. 常见 Python 语法对应含义

| 代码 | 含义 |
|---|---|
| `async def` | 定义可等待的异步函数 |
| `await produce()` | 调用并等待异步阶段逻辑；不会自动替你创建数据库事务 |
| `async with sessions.begin()` | 管理会话/事务进入和退出，包括提交或回滚 |
| `@asynccontextmanager` | 用包含 yield 的异步函数构造异步上下文管理器 |
| `_claim()` 中的 `yield` | 把控制权交给外层执行块，持锁连接暂不关闭 |
| `finally` | 成功、异常等退出路径都尝试执行清理 |
| `history or ()` | 没有历史时使用空元组 |
| `any(...)` | 至少一项符合条件就返回 True |
| `artifact is not None` | 明确判断有阶段产物，不用内容真假猜测是否存在 |
| `uuid4().hex` | 生成新尝试或结果的随机身份，不是稳定 operation_key |

## 15. 当前保证与支持边界

当前入口从已接受计划开始，覆盖首轮知识任务和 Observation；不负责接受计划之前的初始规划恢复，也不扩展多轮 Replanning。

机制能够防止同一 run 的正常并发接续，复用已正式保存的阶段结果，保留不确定尝试，并拒绝失去执行权的旧运行继续提交。

它不保证外部 API 恰好执行一次，不保证历史模型输出的自然语言结论一定正确，不会因为本轮图结束而自动关闭案件，也不自动实现训练、审批、发布的恢复协议。后续有副作用的工具仍需各自的幂等键、状态查询和权限审计。

## 16. 建议阅读顺序

1. `recovery.py::_claim()`：谁获得本轮执行权、何时释放。
2. `recovery.py::_ClaimGuard.check()`：为什么旧运行不能回来提交。
3. `recovery.py::_phase()`：三个短事务与外部调用的分界。
4. `recovery.py::_selection()`：为什么不能任意挑选历史结果。
5. `graph.py::build_investigation_graph()`：唯一拓扑如何按可信 Runtime 分派普通/恢复逻辑。
   8D-2 后业务入口统一为 `nodes.py::task_execution()`、`observation()`，
   `recovery.py::ledger_phase_executor()` 提供账本适配，不再另写任务循环或建图。
6. `persistence/recovery_contracts.py` 与 `recovery_models.py`：运行规格、策略、归属和结果选择。
7. `tests/workflow/test_recovery_postgres.py`：结合真实数据库测试理解中断和并发案例。

相关文档：

- [首轮恢复运行说明](runbooks/first-round-recovery.md)
- [执行账本说明](runbooks/investigation-ledger.md)
- [可信运行上下文说明](runbooks/investigation-runtime.md)
- [持久化与恢复总体设计](workflow-persistence-resume-design.md)

记忆主线：**先查历史；需要执行时先提交 STARTED；事务外调用；最后把结果、COMMITTED 和本轮选择一起提交。运行锁跨越整段执行，行锁只保护各个短事务。**
