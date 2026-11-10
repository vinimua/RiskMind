# 开发经验记录

本文记录本项目开发中实际发生的架构问题。规则以 [AGENTS.md](../AGENTS.md) 为准；以下反例为历史问题的简化示意，不是当前可执行代码。测试链接是验证入口，不表示本文编写时已重新执行，也不表示所有建议断言已经存在。

## 1. 两张 Workflow Graph

**现象**：普通执行与恢复执行曾分别建图，恢复图还有独立的结束节点和 State。

**根因**：把恢复机制误当成另一条业务流程。

**错误示例**：普通流程新增 Observation 规则，恢复流程忘记同步。

**正确示例**：由 [graph.py](../backend/src/risk_platform/workflow/graph.py) 唯一维护主拓扑；Runner、运行控制和阶段执行器处理持久化及恢复。合理子图可以存在，但不能复制 Investigation 主流程。

**对应验证**：查看 [test_graph.py](../backend/tests/workflow/test_graph.py) 和 [test_recovery_postgres.py](../backend/tests/workflow/test_recovery_postgres.py)；审阅首次执行与恢复是否进入相同节点、相同结束语义，而非仅检查两个入口都能返回。

## 2. Normal / Recovery 两套任务循环

**现象**：任务遍历、请求生成、工具调用和结果处理曾分别维护。

**根因**：业务循环与保存、复用、重试机制耦合。

**错误示例**：在恢复分支重新实现 `for task`，随后只修复其中一份身份检查。

**正确示例**：[nodes.py](../backend/src/risk_platform/workflow/nodes.py) 保留业务阶段，[phase_execution.py](../backend/src/risk_platform/workflow/phase_execution.py) 负责阶段入账。结果必须先通过身份校验，再提交账本。

**对应验证**：[test_phase_execution.py](../backend/tests/workflow/test_phase_execution.py)、[test_recovery_postgres.py](../backend/tests/workflow/test_recovery_postgres.py)。检查首次执行调用顺序、已提交结果复用及错误结果不入账。

## 3. Planning 一度放到图外

**现象**：正式执行入口一度要求先在图外生成并接受计划。

**根因**：把执行入口当时要求 AcceptedPlan 的技术限制，当成了业务流程边界。

**错误示例**：应用先调用模型生成计划，再启动一张只处理执行的图。

**正确示例**：图内 `prepare_plan` 生成候选、确定性校验并正式接受；已有正式计划时加载核验。见 [planning.py](../backend/src/risk_platform/workflow/planning.py)。类型为 AcceptedPlan 的内存对象不代表数据库已经接受。

**对应验证**：[test_initial_planning_postgres.py](../backend/tests/workflow/test_initial_planning_postgres.py)。核对候选提交后中断、正式接受后中断、重入复用及未接受计划不能执行。

## 4. 长期保留 direct path

**现象**：同一主图曾同时支持不入账与持久化两种正式调用方式。

**根因**：为了测试方便保留生产路径，没有核查其长期业务价值。

**错误示例**：正式调用因为缺少数据库配置，静默改走直接执行。

**正确示例**：正式调查统一走 [runner.py](../backend/src/risk_platform/workflow/runner.py)；单元测试直接测业务函数，不需要保留无账本的生产入口。

**对应验证**：[test_graph.py](../backend/tests/workflow/test_graph.py)、[test_invocation.py](../backend/tests/workflow/test_invocation.py)。检查缺失正式依赖、未 claim 的调用不能启动业务执行。

## 5. 过多 Context 包装及配置混入身份

**现象**：运行控制曾重复引用可信上下文，并携带模型、Prompt 和知识检索配置。

**根因**：没有先区分数据所有权、生命周期和权威来源，逐次添加字段补问题。

**错误示例**：同一个 actor 在多个 Context 中维护；更换下一阶段模型却被视为修改运行身份。

**正确示例**：可信环境见 [runtime.py](../backend/src/risk_platform/workflow/runtime.py)，运行控制见 [run_guard.py](../backend/src/risk_platform/workflow/run_guard.py)，业务状态见 [state.py](../backend/src/risk_platform/workflow/state.py)。运行身份稳定，阶段协议参与对应操作输入，工具依赖由组合入口显式提供。冻结对象不等于权限永远有效。

**对应验证**：[test_runtime.py](../backend/tests/workflow/test_runtime.py)、[test_stage_protocols_postgres.py](../backend/tests/workflow/test_stage_protocols_postgres.py)、[test_registered_tools.py](../backend/tests/workflow/test_registered_tools.py)。检查权限隔离、阶段协议冲突和依赖来自当前工具绑定。

## 6. 局部阶段反复标记 SEALED

**现象**：局部测试通过后，仍在后续审阅中发现双图、重复循环和入口不完整。

**根因**：阶段验收替代了端到端架构检查，工程结构有效与真实模型语义质量没有始终分开说明。

**错误示例**：工具调用 JSON 测试通过，就宣称调查能力或检索效果已经验证。

**正确示例**：分别审阅输入到 Observation 的主链、真实 PG 事务与恢复、工具实际效果、模型解释质量。文档明确已实现、占位和未验证范围；测试数量不能证明边界正确。

**对应验证**：结合 [test_first_round_execution.py](../backend/tests/workflow/test_first_round_execution.py)、[test_observation.py](../backend/tests/workflow/test_observation.py) 和真实 PG 恢复测试。模型语义质量需另做真实评测，不能从 mock 成功推导。

## 7. 无依据兼容与过度解释校验

**现象**：曾为未确认存在的旧记录保留兼容分支；解释重复校验时过度依赖极端并发场景。

**根因**：没有先调查实际数据，也没有逐项对应所保护的不变量。

**错误示例**：凭“以后可能需要”保留旧业务路径；或反过来，因为正常顺序执行不会触发，就删除提交前的授权与运行权检查。

**正确示例**：兼容范围依据调用方、实际数据、Checkpoint 和部署要求确定。事务内重复读取可评估合并，但跨外部调用和跨事务的权限、运行锁、generation 检查保护不同时间点。再次鉴权只说明检查当时获准，不能宣称消除外部权限变更的全部并发窗口。

**对应验证**：[test_current_record_readers.py](../backend/tests/workflow/test_current_record_readers.py)、[test_access_postgres.py](../backend/tests/workflow/test_access_postgres.py)、[test_recovery_postgres.py](../backend/tests/workflow/test_recovery_postgres.py)。真实旧版本升级、撤权协调及失锁接管的覆盖范围必须分别核实，不把相关测试当成全部场景证明。

## 后续记录方式

新增案例继续按“现象 → 根因 → 错误示例 → 正确示例 → 对应验证”记录。开发前说明目标调用链、数据归属、状态转换、失败与恢复语义；交付时列出实际验证及未完成范围。本文不追认任何阶段已完成，也不授权删除历史数据或兼容路径。
