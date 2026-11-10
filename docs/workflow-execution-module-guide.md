# Workflow 执行边界与代码阅读指南

## 1. 当前边界

| 边界 | 内容 | 创建与变化时机 |
|---|---|---|
| `InvestigationInvocation.runtime` | 当前身份、案件、轮次、授权入口、工具绑定、超时 | 可信应用按次提供，授权结果可能随外部状态变化 |
| `RunExecutionContext.identity` | run / investigation / round / plan ID | Runner 确定，仅表达运行归属 |
| `RunExecutionContext.policy` | 本次重试和历史结果选择规则 | 本次调用明确提供，不是永久运行配置 |
| `RunExecutionContext.guard` | 会话锁连接、持有者身份、generation | Runner claim 后创建，运行权动态检查 |
| State | 计划、任务状态、结果、Observation、轮次标记 | 随 LangGraph 节点推进 |
| 阶段 Operation | 实际模型、提示词或工具协议摘要，以及阶段业务输入 | 对应阶段首次登记时固定 |
| Attempt / Result | 尝试状态、真实执行结果、来源和审计 | 追加保存，终态与结果不能覆盖 |

```python
class RunExecutionContext:
    identity: RunIdentity
    policy: RecoveryPolicy
    guard: RunGuard
```

运行对象没有模型、提示词、工具范围、规划规格或第二份 runtime。旧 `RunSpec` 已移除，而不是再换一个地方保存整轮的模型配置。

## 2. 代码职责

| 文件 | 职责 |
|---|---|
| `runner.py` | 唯一持久化调用入口；授权、确定身份、claim、读取检查点、调用图、返回前对账 |
| `graph.py` | 唯一 LangGraph 拓扑和阶段依赖传递 |
| `nodes.py` | 唯一任务循环与 Observation 业务实现 |
| `planning.py` | 图内规划输入、候选生成、接受与正式计划加载 |
| `phase_execution.py` | 阶段输入构造、账本复用、三段事务和结果选择 |
| `run_guard.py` | 运行会话锁与代次 fencing，不保存模型配置 |
| `runtime.py` / `access.py` | 可信环境、canonical 归属和 READ / CONTINUE 权限 |
| `knowledge_tool.py` | 知识工具的范围、profile、数量限制、授权及客户端依赖 |
| `checkpoints.py` | JSON checkpoint 与已记录执行事实对账，不用当前模型参数伪造历史输入 |
| `persistence/*` | 类型契约、短事务数据库操作和不可变历史 |

`recovery.py` 仅兼容导出旧类名，`FirstRoundRecovery is InvestigationRunner`，没有第二个执行器。当前真正多轮 Replanning 尚未实现，图的 END 不表示案件关闭。

## 3. 完整顺序

```text
可信应用提供 runtime
  ↓
Runner：READ + CONTINUE、运行身份、取得会话锁
  ↓
进入唯一 LangGraph
  ↓
prepare_plan：生成候选 → 校验 → 接受入账，或加载已有正式计划
  ↓
task_execution：Query Formulation → 注册工具执行 → 结果
  ↓
observation：解释与引用校验
  ↓
replanning：当前只结束本轮，案件保持 OPEN
  ↓
返回前再次核对 checkpoint 与账本
  ↓
释放运行会话锁
```

初次规划前可以预留 plan_id；这不代表已有计划内容。规划日志、STARTED 与候选都在进入图后的 prepare_plan 阶段创建。已接受计划不能原地覆盖。

## 4. 模型配置不再固定整轮运行

当前实现的规则：

| 阶段情况 | 恢复时配置变化 | 行为 |
|---|---|---|
| 尚未登记该阶段 Operation | 模型或提示词变化 | 新阶段登记当前协议并执行 |
| 已正式选定提交结果 | 模型或提示词变化 | 读取原协议的结果，不再次调用、不声称由新模型生成 |
| 已有 Operation，但尚未固定结果 | 模型或提示词变化 | 明确输入冲突；不能覆盖旧输入或悄悄重试未知调用 |
| 阶段业务输入或工具范围变化 | 与历史不一致 | 不能借“换模型”绕过业务输入一致性与权限检查 |

例如 Query 已由模型 A 完成、Observation 尚未开始，进程中断后用模型 B 接续：Query 仍关联模型 A 的原始结果，Observation 独立登记模型 B。这是阶段边界的变化，不需要改写 run identity。

如需重做已经固定的逻辑操作，应建立新的明确业务版本或逻辑身份；当前没有增加原地重跑已选结果的 API，也没有自动创建新轮次。

模型阶段的 model_name / prompt_version 已在 Operation 保存。Query 的摘要覆盖工具调用协议，Observation 摘要覆盖当前提示词；摘要不保证模型供应商同名模型行为永不变化。

## 5. 三段事务

```text
事务 1：重新授权、检查 guard、检查操作历史、复用已选结果，或登记/核对输入与结算旧 STARTED
commit

事务 2：重新授权、检查 guard、按显式策略创建新 STARTED 和 run/generation 关联
commit

事务外：调用模型或工具

事务 3：重新授权、检查 guard、Result + Attempt.COMMITTED + Selection 原子提交
commit
```

已选结果复用时，先验证旧 Operation 的原始摘要并检查当前读取权限。仅模型协议不同可复用历史；其余稳定业务输入必须一致。没有选择记录的旧操作仍严格核对全部输入，因此不能给 UNCERTAIN 阶段偷偷换模型。

COMMITTED 表示结果可靠保存，不等于业务成功。FAILED、REJECTED、零命中仍分别保留真实语义。未知外部调用中断保留 STARTED，后续按原规则处理 UNCERTAIN。

## 6. 锁与上下文

运行会话锁跨越整次调用，防止同一 run 被同时推进。事务行锁只持续到当前事务结束。generation 阻止失去运行权的旧 worker 提交。

RunGuard.owner_id 是本次持锁者身份。规划阶段据此构造局部 claim 编号，用于区分本次占用与旧中断占用；它不是计划，也不包含规划配置。

公共入口不能由调用方预填 execution；Runner 取得运行权后创建内部控制，图入口再核对案件、轮次、run ID。真正的执行权仍通过数据库锁和代次检查。

## 7. 0008 迁移与历史兼容

- 新运行保存 schema v2 RunIdentity，只有身份字段。
- 新规划日志保存 schema v2 PlanningInput，只有运行身份、初始规划模型和输入/提示词摘要，不预先固定后续 Query / Observation。
- 运行与规划只支持 v2；旧运行/规划契约和投影分支已删除。读取 v1 记录会明确报错，数据库原文不会被修改。
- 迁移只扩展约束和规划 trigger 的版本判断，不批量改写旧记录。
- 当前 Operation 的 v1/v2 读取协议、结果格式、稳定 ID、锁 key 与 checkpoint schema v2 不变。
- 有 v2 运行或规划记录时拒绝降级，避免丢弃无法由 0007 表达的历史。
- 仍明确拒绝旧拓扑 checkpoint v1；本次没有加入自动拓扑迁移。

迁移链保留到 0008，但升级 schema 不代表应用仍支持读取 v1 运行/规划 payload。当前只接受 v2；没有提供旧记录转换或清理操作。Operation / Result 自身的版本支持不受本次删除影响。

## 8. 建议阅读顺序

先看 graph.py 的业务顺序，再看 runner.py 与 RunExecutionContext；然后读 nodes.py 的业务函数、phase_execution._phase 的事务，最后看 checkpoints 如何只根据已记录 Operation 和 Selection 对账。

知识工具与当前产物协议仍只支持知识检索。本次没有新增其他工具结果、记忆模块、自动重试调度器或多轮调查能力。

## 9. 移除旧契约后的验证边界

旧运行/规划兼容测试已删除，新增单元测试覆盖：v2 记录可读、摘要损坏被拒绝、v1 及未知版本在解析 payload 前被明确拒绝。阶段协议变化、当前格式恢复和数据库约束测试继续保留。

本次不删除或转换数据库记录。若环境中存在 v1 运行或规划日志，当前应用将拒绝恢复它们。

本次删除后的验证：读取器专项 8 passed；backend 全量 518 passed、104 skipped，一条既有依赖弃用警告；`git diff --check` 通过。此次未启用真实 PostgreSQL 集成，不沿用之前的集成测试结果作为本次验证。

进一步阅读：[prepare_plan 事务、锁与恢复详解](workflow-prepare-plan-concurrency-authorization.md)，包含候选与正式计划、事务顺序、防御性检查及具体场景。
