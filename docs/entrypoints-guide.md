# 告警与聊天入口审阅指南

## 当前调用链

告警：POST /investigations/from-alert → 创建/复用案件及第一轮 → 原 InvestigationRunner → 唯一调查图。

聊天：POST /chat/messages → 登记消息 → 读取近期历史及记忆 → 一次 ChatAgent.decide → 保存决定。

- 普通回答或追问：保存回答及非空记忆，返回 ANSWERED。
- 查询进度：目标校验、案件 READ 鉴权、登记 STARTED、查询并保存结果。
- 启动调查：返回 PENDING_CONFIRMATION，用户确认后检查 CREATE/READ/CONTINUE，再调用原 Runner。
- 模型只提出建议；工具、权限和用户确认均由确定性代码处理。没有第二张聊天图或调查任务循环。

## HTTP 接口

### 发送消息

POST /chat/messages

```json
{"request_id":"message-001","message":"调查 credit-v3","model_id":"credit-v3"}
```

conversation_id 可空，首次由服务端分配；后续消息带返回的会话 ID。investigation_id 和 model_id 是可选显式目标，不能同时填写。旧 action 字段已删除，提交时返回 422。同一请求重发必须保持 request_id 和完整输入不变。

统一响应为 EntryActionState：turn_id、conversation_id、status、decision、decision_digest、invocation_id、investigation_id、result。result 是最终 ChatResponse，处理中为空。启动等待确认时的 investigation_id 是预分配身份，不代表案件已创建。

### 确认或取消

POST /chat/turns/{turn_id}/confirmation

```json
{"invocation_id":"使用上一步返回的编号","decision_digest":"使用上一步返回的64位摘要","confirmed":true}
```

confirmed=false 表示取消。确认必须属于当前已认证用户及原建议，不允许更改目标或工具。确认落库后接口执行原调用；重复提交返回原状态，不重复启动。

### 查看状态

GET /chat/turns/{turn_id}

只查询，不执行或重试。会话归属和权限始终检查；读取已保存案件结果还需重新检查案件 READ 权限。

状态码：处理中/等待确认 202；已完成回答、成功执行或取消 200；权限 403、不可访问 404、冲突 409、依赖失败 503；输入校验 422。所有接口都依赖服务端认证，不能从正文或任意身份头取得 actor。

## 事务与恢复边界

消息先提交；模型调用在事务外；决定随后提交。确认单独提交。领取调用的 READY → STARTED 与 started_at 同事务提交，再在事务外执行。最终动作状态及聊天响应原子提交；普通回答的记忆也在完成事务保存。

稳定 turn/call/case 身份与行锁防止同一请求重复领取。取消后不能改为确认。STARTED/UNCERTAIN 不自动重试：进程退出或最终响应保存失败时，按原 turn 查询关联案件，再由授权运维依据原 Runner 核实和接续。没有自动租约接管或入口恢复作业。

确认等待期间，同一会话仍有未结束 turn，其他消息被阻断。没有前端确认页面、确认超时或自然语言“确认”解析。

## 记忆与历史

复用 MemoryContent、MemoryRecord、assemble_context。普通回答与非空记忆原子保存；工具结果不进入普通聊天历史，避免绕过案件权限。近期最多读取十个成功候选 turn，每段历史最多 2000 字符，记忆预算默认 12000 字符；尚未实现累计长期记忆和全 Prompt token 预算。

旧 CHAT/AUTO 历史及意图审计字段保留，只用于读取校验。旧请求不能被重新解释为新入口调用；相同旧请求身份会冲突，不恢复旧分类执行。迁移链与数据库历史均不删除。

## 审阅顺序

1. conversations/contracts.py：消息、模型决定、确认和响应。
2. conversations/agent.py、tools.py：一次模型决策与原生调用校验。
3. entrypoints.py：chat → prepare_entry；confirm_chat → confirm_entry → execute_entry。
4. action_repository.py：短事务、归属、不可变决定、领取和结算。
5. repository.py：聊天记录及历史读取；_LegacyIntent 只解析历史。
6. api/routes/entrypoints.py：三个聊天端点及错误映射。
7. tests/workflow/test_entry_actions_postgres.py：真实数据库及 HTTP 到原 Runner 的链路。

部署仍须注入认证、授权、会话工厂、Runner、ChatAgent 和可信工具工厂。未配置时返回 503。业务数据库迁移未在本次开发中执行。
