# 图内初始规划与接受：实现和恢复边界

本次将候选计划生成放回唯一 LangGraph。保留唯一正式入口、持久化阶段与现有知识检索业务；不加入第二个编排器或多轮 Replanning。

## 1. 调用方式

可信应用先建立 Investigation、Round，并从 canonical 记录加载已认证的 runtime，然后：

```python
runner = FirstRoundRecovery(agent, session_factory=sessions, checkpointer=saver)
result = await runner.ainvoke(context=InvestigationInvocation(runtime=runtime))
run_id, plan_id = result["run_id"], result["plan_id"]
```

用户无需先传 plan_id，也不需要 Fresh/Resume 开关。仍可以显式提供已有 plan_id；若一轮存在多份正式计划且还未绑定 Run，必须明确选择，不能自动取最新。

入口不负责检测异常、登录认证或自动新建案件。告警和规划输入已保存在 canonical Investigation 中，模型不能修改这些可信身份。

## 2. 唯一图

```text
START
  ↓
prepare_plan
  ├─ AcceptedPlan 已存在 → 核验并加载
  └─ 不存在
       → initial_plan() 生成候选
       → 确定性校验
       → 候选可靠入账
       → accept_plan() 正式接受
  ↓
task_execution → observation → replanning → END
```

prepare_plan 内复用 nodes.initial_planning / InvestigationAgent.initial_plan。模型只产生目标和任务草案；其 response 必须完整 finish_reason=stop。任务 ID 由系统构造，可靠保存后不会因节点重入重新生成。

确定性校验包括：计划和任务非空、goal/objective/expected_result 非空白、合法且唯一的 task_id、全部 PENDING、能力为当前 canonical 输入允许的 KNOWLEDGE_RETRIEVAL。正式计划的案件/轮次/身份来自可信上下文与内部预留身份，不来自模型输出。

## 3. 为什么新增规划日志

原 Operation 和 Execution Run 都有 AcceptedPlan FK，不能在计划尚不存在时写入。保持原约束不变，新增：

| 表 | 职责 |
|---|---|
| investigation_planning_runs | 案件/轮次对应的稳定规划规格与内部预留身份，输入不可变 |
| investigation_planning_attempts | STARTED / COMMITTED / FAILED / UNCERTAIN，候选正文与摘要，actor 与运行归因 |
| investigation_planning_acceptances | 哪份已提交候选被正式接受、由谁接受，并关联 AcceptedPlan |

它们只记录 prepare_plan 的持久化事实，不调度任务，不构成第二套 Workflow。

新 run_id 只由案件和轮次确定；预留 plan_id 只是稳定身份，不等于 AcceptedPlan 已存在。规划规格绑定 canonical 输入摘要、模型、初始提示词摘要以及原执行规格。actor/timeout 放在尝试归因中，不能通过更换操作者修改稳定输入。

## 4. 规划与接受的事务

外层会话级 run 锁覆盖整次图调用。计划尚未正式接受前，不创建依赖 AcceptedPlan 的 Execution Run。

```text
规划事务 1：读取历史
  已有 COMMITTED 候选 → 复用
  旧 claim 的 STARTED → UNCERTAIN
  提交

规划事务 2：检查显式 retry policy
  创建新 STARTED + actor/runtime 归因
  提交

事务外：调用规划模型，校验候选

规划事务 3：候选正文 + 摘要 + COMMITTED
  一起提交

接受事务：重新鉴权、核验锁和候选
  AcceptedPlan + 规范化任务
  PlanningAcceptance 来源与接受人
  Execution Run + generation / RunClaim
  一起提交

节点返回计划，LangGraph 保存 checkpoint
```

只有接受事务成功后才允许 Query / Knowledge / Observation 的原 _phase() 开始。其三个事务协议不变。

候选生成者和正式接受者可能不同，分别保留 actor。接受时重新读取候选、校验摘要与业务约束；数据库还检查正式计划内容与已提交候选完全一致。

## 5. 崩溃与失败

| 位置 | 恢复行为 |
|---|---|
| STARTED 前 | 无外部调用，重新检查历史 |
| STARTED 后、候选提交前 | 旧尝试为 UNCERTAIN，默认阻断，显式允许才重试 |
| 候选 COMMITTED 后、接受前 | 复用候选及原 task_id，不重新调用模型 |
| 接受后、节点 checkpoint 前 | 加载正式计划，避免重复接受/规划 |
| 后续 Task 中断 | 原阶段账本恢复机制继续生效 |

确定非法的候选记录 FAILED；默认不自动重试。超时、依赖异常或进程未知中断按保守不确定边界处理。取消传播，不假装生成成功，不回退 objective 或空计划。

规划锁连接丢失后，旧 worker 不能在新 worker 完成后提交候选。尝试终态不可覆盖，同一规划只允许一份 COMMITTED 候选；不存在自动挑选最新成功的策略。

## 6. 兼容与部署

新增 Alembic 0007，为增量迁移；旧 0004/0005/0006 不改写。现有 AcceptedPlan / Operation / Attempt / Result 和 run_id 保留，已有计划继续跳过初始规划模型。

图仍为四个节点，JSON checkpoint 仍为 v2。恢复检查点在尚无 AcceptedPlan 时只允许声明 bootstrap 身份，不能声称已有任务结果、Observation 或本轮完成。

使用新入口前需要将目标业务库升级至 0007。本次只在 UUID 测试 schema 上执行迁移，未升级生产或业务 schema。旧 v1 checkpoint 自动迁移、rolling deployment、run-wide retry 细化和 hung-worker lease 不在本次范围。

## 7. 验证

规划专项覆盖无 plan_id 首次运行、旧计划自动加载、候选/接受两处提交后崩溃、非法计划、显式重试、权限撤销、并发启动、连接丢失 fencing、数据库不可变、接受内容错配以及真实 os._exit 后恢复。

代码入口：recovery.ainvoke → _claim → graph.prepare_plan → recovery.prepare_plan → _planning_candidate → planning_repository。

实际验证：规划专项 17 passed；最终 backend 全量启用真实 PostgreSQL，543 passed、7 skipped，
1 条已有依赖弃用警告。包含新规划候选和旧知识执行阶段的真实 os._exit 恢复测试。
git diff --check、Python 语法与新增文件空白检查通过。测试未调用付费模型 API。
