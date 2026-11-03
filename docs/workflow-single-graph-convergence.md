# Stage 8D-1：统一 Workflow Topology

状态：已实现，待 review。本次只收敛拓扑，不进入 Stage 9，不合并普通执行与恢复执行的阶段协议。

后续状态：8D-1 已 review SEALED；本文保留该阶段的结构说明。
8D-2 已进一步删除独立恢复任务循环，当前实现见 [阶段执行收敛](workflow-phase-execution-convergence.md)。
8D-3 又删除裸 Runtime 自动包装；下文为 8D-1 阶段记录，当前入口见 [统一调用契约](workflow-invocation-convergence.md)。

## 1. 唯一拓扑

`workflow/graph.py::build_investigation_graph()` 是唯一创建 `StateGraph`、注册节点和连接边的地方：

```text
START → prepare_plan → task_execution → observation → replanning
                            ↑                           │
                            └──── round_complete=False ─┤
                                                       └─ round_complete=True → END
```

当前 replanning 仍然只返回本轮结束、案件 OPEN，不实现多轮调查。保留条件边是原有普通图的结构，不意味着新实现了重规划能力。

普通入口和 `FirstRoundRecovery` 使用同一 builder、同一节点名和同一拓扑定义；服务可以创建不同 compiled graph 实例，不要求共享一个全局对象。

## 2. 不同执行机制放在哪里

| 节点 | 普通执行 | 受控恢复执行 |
|---|---|---|
| prepare_plan | 原 initial_planning | recovery.load_accepted_plan，绝不重新调用规划模型 |
| task_execution | 原 nodes.task_execution | recovery.recoverable_task_execution，各阶段通过 _phase |
| observation | 原 nodes.observation | recovery.recoverable_observation，通过 _phase |
| replanning | nodes.replanning | 同一个 nodes.replanning |

恢复模块不再拥有 `_build_graph()`、独立 `RecoveryState` 或 `finish_round` 节点。
`_claim()`、`_phase()`、`_selection()`、fencing、账本检查和检查点验证仍保留。
三个事务的执行语义没有因为移动节点实现而改变。

## 3. Runtime 与 State

`invocation.py` 定义可信包装：

```python
@dataclass(frozen=True)
class InvestigationInvocation:
    runtime: InvestigationRuntimeContext
    recovery: "_Invocation | None" = None
```

普通调用可以继续使用 8B 的 `context=InvestigationRuntimeContext(...)`，节点内部将其适配为没有 recovery 的包装。
恢复入口负责取得运行锁后构造包装，不接受客户端通过 State 开启恢复模式。
包装中的基础 runtime 和恢复调用里的身份上下文必须为同一对象，否则拒绝。

`InvestigationState` 成为唯一图状态定义，业务字段允许类型化对象或 JSON wire 数据：

- 普通路径保留原有 Pydantic 对象形式。
- 恢复路径保留 JSON-only 检查点形式，恢复函数重新校验为业务对象。
- 可选 checkpoint_schema_version、run_id、plan_id 只用于身份核验，不授予权限或决定执行模式。
- actor、授权函数、运行锁和恢复策略不写入检查点。

这种联合字段是本次小步收敛的过渡边界，不代表两条路径已经拥有统一序列化或 phase 协议。
为普通调用传入 JSON-only saver，也不会自动让普通路径获得恢复能力。

## 4. 检查点兼容

新恢复检查点使用 `checkpoint_schema_version=2`，标记统一后的拓扑。
旧 8C v1 明确拒绝，不自动转换。未知或旧节点名同样拒绝，不静默跳过。

原因是检查点不仅有 JSON 业务字段，还包含待执行节点与图推进信息。
仅修改节点名称或声明字段兼容，不能证明旧流程可以安全接续。

本次没有提供迁移工具，也没有重写旧检查点、删除历史或改换 thread_id 来绕过核验。
部署时需要识别在途运行，制定单独的显式迁移安排；本版本不能宣称与旧拓扑滚动兼容。
数据库 ledger schema、旧账本输入摘要与历史 Result 不变，无新增 Alembic 迁移。

## 5. 测试重点

保留原有普通执行、鉴权、恢复、结果复用、并发独占、连接丢失 fencing 和真实 os._exit 测试。
新增验证：

1. 图只有 prepare_plan / task_execution / observation / replanning 四个业务节点。
2. recovery 模块不再构造 StateGraph 或声明 RecoveryState。
3. 可信包装下普通执行行为保持；State 伪造 mode 或身份字段不能开启恢复路径。
4. 恢复路径确实调用共享 replanning。
5. 旧拓扑节点明确拒绝，且不调用外部服务、不创建新 Attempt。
6. 持久化检查点被标记为旧 v1 后明确拒绝，不重新调用模型或检索。

## 6. 保留的下一步

8D-2 才考虑普通/恢复 phase execution 收敛；8D-3 才考虑 Fresh/Resume 的统一入口语义。
当前普通入口没有账本去重保证，不应被用来重新执行同一已入账轮次并声称安全恢复。

代码阅读顺序：`graph.py` → `invocation.py` → `state.py` → `recovery.py` 的三个恢复执行函数 → `_phase()`。

事务详解见 [恢复阶段与三个事务](workflow-recovery-phase-guide.md)，运行方式见 [首轮恢复说明](runbooks/first-round-recovery.md)。
