# 移除非持久化调查入口

本次范围：只删除“规划输入 → Agent 生成计划 → 无账本直接执行”这条正式调查路径。
不恢复此前取消的 PlanningService / CandidatePlan 改造，不新增计划接受机制，也不做多轮 Replanning。

后续扩展：规划已重新纳入唯一图，新增了候选日志和确定性接受，不恢复 direct 路径。
本文记录删除非持久化入口时的边界；当前调用方式见 [图内初始规划](workflow-initial-planning-recovery.md)。

## 唯一正式入口

```python
runner = FirstRoundRecovery(agent, session_factory=sessions, checkpointer=saver)
result = await runner.ainvoke(
    plan_id=accepted_plan_id,
    context=InvestigationInvocation(runtime=trusted_runtime),
)
```

同一接口处理首次执行、恢复、复用或阻断，不需要 Fresh/Resume 开关。调用前必须已有正式入账的 Investigation、Round 和 AcceptedPlan。
候选计划不能直接作为执行输入；当前仍由可信调用方使用现有仓储 accept_plan 提交正式计划。本次不新增或宣称完成上层计划生成/接受服务。

## 代码变化

- graph.py 不再引用 initial_planning，prepare_plan 永远读取 Accepted Plan。
- 构图必须提供 session_factory 和 checkpointer；裸图不接受没有内部 claim 的调用。
- require_invocation(claimed=True) 必须看到 runner 鉴权、加锁后生成的内部 claim，不能仅靠 State 中的 mode/run_id/plan_id 切换路径。
- nodes.py 删除 direct_phase；task_execution 和 observation 不再提供默认阶段执行器，生产图始终注入 ledger_phase_executor。
- 单元测试显式使用 tests/workflow/workflow_test_helpers.py 中的阶段替身，直接测试业务函数；生产模块不导入测试代码。

build_investigation_graph 仍是唯一拓扑构造函数，但只是 runner 的内部实现，不再是一条无账本业务入口。
保留 nodes.initial_planning 等可独立测试的业务函数，不意味着恢复了可跳过 AcceptedPlan 的正式调查路径。

## 保持不变

_phase 的三个事务、READ/CONTINUE 分权、generation fencing、显式 retry、多结果选择和历史不可变保持不变。
数据库 schema、operation digest、checkpoint v2、正式节点顺序不变；旧 v1 checkpoint 拒绝策略保持。
本轮结束仍为 round_complete=True / investigation_status=OPEN，不表示案件完成。

## 调用迁移

旧的 build_investigation_graph(agent).ainvoke(planning_input, ...) 不再支持。
调用方需要先取得正式 plan_id，再使用 runner；不设置默认计划、不自动接受模型输出、不静默回退为 direct。
此调用契约变更需要同步应用代码，不承诺跨旧版本滚动兼容。

测试重点：拒绝无 claim 直接调用、拒绝缺少持久化依赖、拒绝缺省执行器，并继续验证真实 PostgreSQL 首次执行/恢复与进程退出场景。

本次验证：focused 81 passed；启用真实 PostgreSQL 的 backend 全量 523 passed、7 skipped，
包含真实 os._exit 恢复测试；1 条已有依赖弃用警告。git diff --check 通过。
