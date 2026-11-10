# 告警与聊天入口审阅指南

## 两条调用链

已发现异常 → `POST /investigations/from-alert` → `EntryServices.from_alert()` → 创建 Investigation / Round 1 → `InvestigationRunner.ainvoke()` → 唯一 LangGraph → 图内规划、确定性接受、工具执行、Observation、账本和案件记忆。

用户消息 → `POST /chat/messages` → 服务端认证 → 会话归属/授权检查 → 保存 STARTED 和用户消息 → 按明确 action 处理 → 重新检查会话权限 → 同事务保存回答和聊天记忆。

入口不会计算监测指标或判定阈值；告警必须由监测服务先确认。普通聊天不持有工具、不调用调查执行函数。

## 代码阅读顺序

1. `api/routes/entrypoints.py`：认证、请求校验、错误映射。
2. `entrypoints.py`：应用入口与 Runner 的连接。
3. `conversations/contracts.py`：客户端可提交的字段及动作约束。
4. `conversations/repository.py`：消息登记、重复请求、历史和记忆读取、完成提交。
5. `conversations/agent.py`：普通问答模型及记忆候选；不负责授权或启动调查。
6. `conversations/models.py`、迁移 0015：数据库关系和不可变约束。

## 请求示例

告警请求：
```json
{"alert_id":"alert-001","model_id":"credit-v3","metric":"PSI","symptom":"分布漂移","time_window":"2026-11-10","current_value":0.3,"reference_value":0.1,"triggered_at":"2026-11-10T08:00:00+08:00"}
```

普通聊天：
```json
{"request_id":"message-001","message":"以后请用中文回答","action":"CHAT"}
```

首次返回 conversation_id，后续新消息带上它，并使用新的 request_id。同一次请求重发必须保持原始请求体不变。

查询案件只要求案件 READ 权限，不推出 CONTINUE 权限：
```json
{"request_id":"message-002","conversation_id":"返回的会话ID","message":"调查进度？","action":"READ_INVESTIGATION","investigation_id":"案件ID"}
```

明确启动新调查：
```json
{"request_id":"message-003","conversation_id":"返回的会话ID","message":"排查模型分布漂移","action":"START_INVESTIGATION","model_id":"credit-v3"}
```

action 由调用界面或可信应用明确选择；当前不实现自然语言意图自动路由。查询只输出任务状态和 Observation 状态，不返回知识正文或根因结论。普通聊天上下文不加载案件查询/执行动作产生的回答，避免隐式跨越案件权限。

## 身份与依赖配置

`create_app(entry_services=services, authenticate=authenticate)` 是部署组合入口。认证函数接收 FastAPI Request，返回真实 actor_id（也支持异步函数）；应接入实际登录态或网关身份验证，不能直接信任客户端任意身份头。

`EntryServices` 显式注入：会话工厂、已配置的 InvestigationRunner、ChatAgent、tools_for(actor, planning_input)、案件授权函数、会话授权函数。Runner 与客户端生命周期由应用组合层管理；本文没有新增第二套调度器或默认允许所有人的策略。

案件授权动作：`investigation:create`、现有 `investigation:read`、`investigation:continue`。会话动作：`chat:create`、`chat:read`、`chat:send`。回调必须明确返回 True；异常按拒绝处理。工具由可信工厂绑定到实际 actor/model scope，不能从聊天 JSON 注入。

未提供认证或业务依赖时，默认应用的两个业务端点返回 503，健康检查仍可用。这是可组合的后端实现，尚未附带登录系统、前端或部署配置。应用业务库之前需按项目流程执行 Alembic 迁移；本次开发测试只迁移隔离测试 schema。

## 幂等、并发和事务

- 同 alert_id 映射到同一案件；相同规范输入复用，不同输入冲突。原创建者保留，不因后续调用者改变。重发仍需 CREATE、READ、CONTINUE 及工具授权。
- 聊天 turn_id 来自 actor_id + request_id；同请求复用完成结果，不再次调用模型；同 ID 不同内容冲突。
- 会话只允许其 owner 访问，同时还须通过当前授权回调。
- 同一会话已有 STARTED 时，其他请求返回 409；数据库部分唯一索引兜底。事务锁仅保护登记，不跨模型调用持有。
- 用户消息先提交，随后在事务外调用模型/Runner；回答与记忆同事务提交，不能出现只保存其中一半。
- 已完成 turn 禁止修改或删除，原始输入不可改。失败回答同样是终态，不自动重试。
- 进程退出、取消、权限撤销导致无法结算时会留下 STARTED，当前明确阻断，不声称已实现聊天恢复/租约接管。调查内部恢复仍由原 Runner 和账本处理。

## 聊天记忆范围

一轮普通问答的单次模型调用返回 answer 和 memory 候选；没有值得保存的信息时 memory 可以为空。它复用 MemoryContent 分类与 AgentContext 组装器，不复制案件记忆系统。

聊天记忆与回答保存在同一 turn，来源 CHAT_SESSION 指向 turn_id，scope 为 CONVERSATION。它不是已验证执行事实，不能生成 execution_summaries。模型提取存在语义误差；当前没有人工审核、跨会话长期记忆合并或秘密识别保证。

下一次普通聊天读取最近 10 个成功普通问答及对应记忆；每条历史正文最多 2000 字符，记忆组装预算为 12000 字符。这里没有实现无限历史召回、自动摘要或完整模型 token 预算，超出近期范围的记忆目前不会召回。聊天不要求恢复同一次模型调用的固定上下文快照；案件执行的快照和恢复机制保持原样。

## 验收关注点

重复告警不重复执行；聊天明确发起调查经过同一个真实 Runner；纯查询不要求 CONTINUE；普通文本要求“开始调查”也不会绕过显式动作；会话串用拒绝；相同 request_id 输入冲突；回答与记忆原子保存；STARTED 阻断重复；数据库拒绝篡改终态。
