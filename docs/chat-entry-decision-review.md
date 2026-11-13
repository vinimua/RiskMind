# 单次聊天入口决策审阅

当前已完成三个阶段：模型协议、Action Gate 与可靠登记、HTTP 切换及旧执行路径删除。

## 目标与实际链路

POST /chat/messages → EntryServices.chat → prepare_entry → ChatAgent.decide → parse_entry_decision → 目标校验 → 保存决定。

普通回答直接完成；查询进度调用 execute_entry；启动调查等待显式确认。

POST /chat/turns/{turn_id}/confirmation → confirm_chat → confirm_entry（提交确认）→ execute_entry（领取并交给原 Runner）。

GET /chat/turns/{turn_id} → inspect_entry，只读取。

## 本次删除

删除 ChatRequest.action、IntentDraft、ChatAgent.identify_intent/reply、旧提示词、conversations/intent.py、EntryServices.chat 中旧分类分派及 record_intent 写入函数。观测只记录一次 entry-decision，不再创建意图分类 generation。

保留数据库历史 action/intent_payload/intent_digest、既有迁移和最小 _LegacyIntent 读取器。这些没有模型调用或执行分派，不构成第二条生产执行路径。新 API 拒绝旧 action 参数，旧 turn 不转换为新决定。

## 数据归属和事务

聊天记录保存消息和响应；入口决定保存建议、确认及交接状态；案件和阶段结果仍以原调查账本为准。decision_digest 固定确认对象，invocation_id 与案件 ID 由服务端按 turn 稳定生成。

先提交消息，再调用模型；决定、确认、领取分别在短事务保存；工具与 Runner 均在事务外执行；最终响应和动作状态同事务提交。普通回答与记忆原子提交。

STARTED 表示已领取，不证明完成。未知异常记为 UNCERTAIN，取消/崩溃/落库失败可能留下 STARTED；入口不自动重试。原 turn 保留稳定案件关联，授权运维可核实并通过原 Runner 接续。

## 审阅文件

- api/routes/entrypoints.py：消息、确认/取消、查询状态。
- entrypoints.py：chat、confirm_chat、prepare_entry、execute_entry、inspect_entry。
- conversations/contracts.py：请求与响应。
- conversations/action_repository.py：状态转换及事务不变量。
- tests/test_entrypoints.py：HTTP 输入、认证、状态码。
- tests/workflow/test_entry_actions_postgres.py：真实 PG、HTTP、确认、并发及原 Runner。

具体请求示例见 [入口指南](entrypoints-guide.md)。尚未实现前端页面、自动恢复、确认过期和累计长期聊天记忆。目标校验不证明模型理解正确，真实模型对否定、假设、多动作的质量需另行评测。

## 本阶段验证

默认测试 620 passed、120 skipped；真实 PostgreSQL 入口与动作回归 11 passed。测试覆盖 HTTP 到原 Runner、确认/取消、重复请求、并发领取、权限撤销、未知结果、响应保存失败以及旧历史迁移读取。已有一条 LangGraph 依赖弃用警告。仅迁移隔离测试 schema，未操作业务库；临时测试实例已停止，未提交 Git。
