# 从一条聊天消息看懂 RiskMind：认证、记忆、模型决策与可靠执行

日期：2026-11-15。依据：当前仓库代码。阅读范围：`POST /chat/messages` 到入口动作完成；调查图内部只说明交接位置。

本文是对现有实现的解释。代码节选中的注释用于讲解；标为“示意”的内容不代表真实请求记录或已运行结果。文中的代码链接面向当前本地工作区，后续代码变动可能使行号变化。

## 1. 先带着一个问题读代码

用户发送：“请对 credit-v3 启动调查，排查近期的特征处理规则变更。”

后端要依次回答：请求来自谁？这是不是重发？哪些历史可以读取？模型是在回答还是请求工具？目标是否明确？是否需要确认？实际执行到了哪里？

这些问题分别由不同代码负责，不能全部交给模型。

```text
HTTP 请求
  → principal：取得认证身份 actor
  → HTTP chat：解析后的消息交给 EntryServices
  → EntryServices.chat
      → prepare_entry
          → 提交用户消息／复用已有状态
          → 读取近期问答与记忆
          → ChatAgent.decide：一次模型决策
          → 保存回答或工具建议
      ├─ 普通回答／追问：返回结果
      ├─ 查询进度：READY → execute_entry
      └─ 启动调查：PENDING_CONFIRMATION → 等待确认

POST /chat/turns/{turn_id}/confirmation
  → confirm_entry：固定确认对象与确认事实
  → execute_entry：提交 STARTED，再执行原业务服务
  → 保存执行状态与最终聊天响应
```

先记住四个函数的分工：

| 函数 | 输入 | 返回 | 是否执行工具 |
|---|---|---|---|
| `prepare_entry()` | `ChatRequest`、认证后的 `actor` | `EntryActionState` | 否 |
| `ChatAgent.decide()` | 消息、历史、所选记忆、工具声明及明确目标 | `ChatDraft` 或 `ToolInvocation` | 否 |
| `execute_entry()` | 已保存请求的 `turn_id`、`actor` | `EntryActionState` | 条件满足时执行 |
| `EntryServices.chat()` | 请求和 `actor` | `EntryActionState` | 准备结果为 `READY` 时继续执行 |

## 2. HTTP chat：`app.state` 与 `Depends` 到底是什么

入口：[HTTP chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:56)。

```python
@router.post("/chat/messages")
async def chat(body: ChatRequest, request: Request,
               actor: str = Depends(principal)):
    return entry_response(
        await invoke(
            request.app.state.entry_services.chat(body, actor=actor)
        )
    )
```

这段代码包含三种参数来源：`body` 来自请求 JSON 并接受 Pydantic 校验；`request` 是框架提供的当前 HTTP 请求对象；`actor` 来自依赖函数 `principal`，不是客户端 JSON 中自报的用户名。请求模型不允许额外字段，因此客户端额外提交一个 `actor` 也不会取得执行身份。

### 2.1 逐段拆开 `request.app.state.entry_services`

| 表达式 | 当前代码中的含义 |
|---|---|
| `request` | 本次 HTTP 请求 |
| `request.app` | 处理该请求的 FastAPI 应用对象 |
| `request.app.state` | 应用实例持有的共享属性容器 |
| `.entry_services` | 启动时安装进去的 `EntryServices` 实例 |
| `.chat(...)` | 调用这个实例的聊天业务方法 |

`state` 不是数据库，也不是某个用户的会话。它是应用保存服务引用的位置。赋值位置在 [create_app()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/main.py:14)：可以由调用方直接注入服务，也可以在 lifespan 中进入服务上下文、取得服务后安装。

```python
# 含义示意：安装已经构造好的依赖。
app.state.entry_services = entry_services
app.state.authenticate = authenticate

# 收到请求后取得相同的业务服务引用。
services = request.app.state.entry_services
```

这里的点号是 Python 属性访问语法。`entry_services` 是本项目选定的属性名，不是 FastAPI 内置的聊天功能。[FastAPI 的 state 属性说明](https://fastapi.tiangolo.com/reference/fastapi/#fastapi.FastAPI.state)介绍了这个应用级容器。

多个请求可以共享服务对象，但各自的 `body`、`actor`、`turn_id` 仍在调用参数和局部变量中。不能把“当前用户”写成 `app.state.actor`，否则并发请求可能互相覆盖身份。多进程部署时，每个 worker 有自己的应用对象；`app.state` 也不能代替跨进程共享数据库。

另外几个同名“状态”需要分清：

| 状态 | 生命周期和用途 |
|---|---|
| `app.state` | 应用级依赖引用，通常随进程启动、退出 |
| `request.state` | 请求级临时属性；本路由没有用它保存会话 |
| `EntryActionState` | 从数据库组合出的聊天动作视图 |
| LangGraph State | 调查流程当前业务数据，与这里的应用属性容器不同 |

### 2.2 `actor: str = Depends(principal)` 不是普通默认字符串

`actor: str` 是类型标注；`Depends(principal)` 是依赖声明。框架在进入路由处理函数前解析依赖，把 `principal` 的返回值交给 `actor`。传入的是函数对象 `principal`，不要写成 `Depends(principal())`。后者会在定义路由时调用函数，而不是让框架针对请求调用。[FastAPI 依赖注入说明](https://fastapi.tiangolo.com/tutorial/dependencies/)

```python
# 含义示意，不是 FastAPI 完整内部实现。
actor = await principal(request)
return await chat(body=validated_body, request=request, actor=actor)
```

认证位置：[principal()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:16)。

```python
authenticate = request.app.state.authenticate
actor = authenticate(request)
if inspect.isawaitable(actor):
    actor = await actor
return TypeAdapter(Identity).validate_python(actor)
```

当前路由支持同步认证回调，也支持返回 awaitable 的回调；本地组合入口安装的是同步回调。`TypeAdapter` 检查返回值符合身份格式，不负责验证密码或 Token 的真实性，真实性应由 `authenticate` 完成。

依赖未配置时返回 503；认证回调失败或返回非法身份时返回 401。认证失败不会进入聊天业务。请求体校验和依赖解析都由框架处理，不应把本文的阅读顺序理解成所有错误响应之间的严格优先级。

### 2.3 从 HTTP 路由跳到业务方法，没有再发一次 HTTP

```python
request.app.state.entry_services.chat(body, actor=actor)
```

这是进程内 Python 方法调用。由于业务方法是 `async def`，调用先产生协程对象，`invoke()` 内的 `await operation` 才等待其执行。`invoke()` 统一把业务异常映射为 HTTP 错误；`entry_response()` 再把正常返回的业务视图编码为 JSON，并选择 HTTP 状态码。

业务方法签名里的 `*, actor` 表示 `actor` 必须以关键字传递。它让调用方明确区分请求体与可信身份：`chat(body, actor=actor)`。

## 3. 单用户与多用户：共享服务不等于共享身份

当前本地组合入口在 [LocalSettings.authenticate()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/bootstrap.py:78)。它读取 `Authorization: Bearer <token>`，核对配置中的本地 Token，成功后返回固定的 `actor_id`。

这个 Token 是访问 RiskMind HTTP 接口的凭证；DeepSeek API Key 是后端调用模型的凭证，两者用途不同。`actor` 是认证结果中的用户 ID，不是任意一种密钥，也不是 Agent。

认证和授权分两层：

| 层次 | 要回答的问题 | 当前实现 |
|---|---|---|
| 认证 | 你是谁？ | 本地 Bearer Token → 固定用户 |
| 会话授权 | 能否创建、读取、发送这段会话？ | 授权回调，加数据库会话 owner 检查 |
| 案件授权 | 能否对该模型创建、读取、继续调查？ | 独立资源授权，并限制配置允许的模型范围 |
| 工具数据授权 | 可以查哪些知识资料？ | 工具绑定中的范围和授权检查 |

单用户不代表只会有一个请求：双击、多个标签页、网络重试仍可能并发。多用户也不要求为每个用户构造一个应用或聊天服务。可以共享无用户临时状态的服务，由认证函数为每次请求产生不同的 `actor`，再在所有读取、执行边界检查归属与权限。

但当前本地启动方式不是完整多用户系统。虽然已有 owner 隔离和授权注入点，企业登录、令牌签名与有效期验证、账号生命周期、租户及团队共享会话等并未因此自动实现。现有 `owner_id == actor` 检查也意味着“多人共享同一会话”还不是当前能力。

## 4. 四种编号：消息重试为什么不能重新编号

编号生成代码：[stable_id()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/repository.py:35)。它对规范分隔的 JSON 参数计算 SHA-256，再拼接前缀；哈希编号不是权限凭证。

| 编号 | 谁产生 | 标识什么 |
|---|---|---|
| `request_id` | 客户端 | 一次逻辑消息提交 |
| `conversation_id` | 首次通常由服务端产生，后续客户端沿用 | 整段会话 |
| `turn_id` | 服务端根据 `actor + request_id` 稳定生成 | 一次聊天请求及其结果 |
| `invocation_id` | 服务端根据 `turn_id` 稳定生成 | 该请求对应的入口工具动作 |

首次请求可以是：

```json
{
  "request_id": "req-001",
  "message": "请对 credit-v3 启动调查",
  "model_id": "credit-v3"
}
```

未传 `conversation_id` 时，服务端根据本次用户和请求编号生成会话 ID。后续新消息使用返回的会话 ID，同时换一个新的 `request_id`。

| 场景 | 正确处理 |
|---|---|
| 在原会话发送新消息 | 原 `conversation_id`，新 `request_id` |
| 原请求超时或响应丢失 | 原 `request_id`，完整原请求内容不变 |
| 确认启动调查 | 使用原 `turn_id`，并提交原 `invocation_id` 和 `decision_digest` |
| 用户主动再次要求另一次调查 | 新逻辑请求，不能当作原请求的网络重试 |

请求摘要比较的是完整 `ChatRequest`，不只是 `message`。例如第一次没有传 `conversation_id`，重试时却补成服务端生成的值，规范化请求已经不同，可能得到 409。客户端应保留原始逻辑请求并原样重试；不要只保留一句正文。

`turn_id` 的计算不包含 `conversation_id`。同一个用户把同一个 `request_id` 换会话提交，也不能绕过去创建另一条请求。不同用户使用相同的客户端请求编号，则得到不同的服务端 turn；最终仍须检查会话 owner。

`ChatRequest` 还禁止同时明确绑定 `investigation_id` 和 `model_id`：前者指向已有案件，后者用于指定新调查目标。格式校验、目标关联、用户授权是三层不同检查。

## 5. `EntryServices.chat()`：先准备，只有 READY 才执行

代码：[EntryServices.chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:130)。

```python
@trace_chat
async def chat(self, request, *, actor):
    view = await self.prepare_entry(request, actor=actor)
    if view.status == "READY":
        return await self.execute_entry(view.turn_id, actor=actor)
    return view
```

它没有自己写一套模型路由分类，也没有启动第二张调查图。普通回答在准备阶段已经完成；查询进度准备好后立即执行；启动调查准备好后仍须等待确认。

`@trace_chat` 是装饰器，给本次聊天操作建立观测范围，并记录返回状态。它不是业务状态存储，也不替代事务。确认路由走 `confirm_chat()`，不能仅凭这里的装饰器就说所有确认请求也被同一个聊天根 trace 覆盖。

## 6. `prepare_entry()`：提交消息、读取材料、调用模型、保存决定

代码：[prepare_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:144)。它返回 `EntryActionState`，不是裸模型响应，也不是 `ChatDraft` 本身。

### 6.1 第一个短事务：消息先可靠保存

```python
async with self.sessions.begin() as session:
    turn, existing = await actions.begin(
        session, request, actor=actor,
        authorize=self.authorize_conversation,
    )
    turn_id, conversation_id = turn.turn_id, turn.conversation_id
    if existing is not None:
        await self._authorize_entry_result(session, actor, existing)
        return existing
```

`actions.begin()` 的内部调用顺序是：

```text
按稳定 turn_id 加事务锁
  ├─ 已有请求：核对归属、完整请求摘要，读取已有动作状态
  │    ├─ 仍为 DECIDING：409，不再次调用模型
  │    └─ 已有决定／结果：返回已有状态
  └─ 新请求：chat.claim_turn()
       → 必要时创建会话
       → 检查 owner、chat:read、chat:send
       → 检查同会话是否已有未完成 turn
       → 插入 ChatTurnRow(status="STARTED", user_text=...)
```

退出 `sessions.begin()` 正常提交之后，代码才进入模型部分。`flush()` 只是把写入发给数据库并检查约束，不等于事务已经提交。

这里保存的是本次用户原文、请求摘要、身份、会话关联和开始状态。没有模型回答，也没有“调查已经启动”的事实。

### 6.2 第二步：读取历史和记忆，关闭读会话后调用模型

```python
async with self.sessions() as session:
    await actions.owned_turn(...)
    history, memories = await chat_store.history_and_memories(
        session, conversation_id, actor,
    )

context = assemble_context(
    scope=MemoryScope(kind="CONVERSATION", id=conversation_id),
    candidates=memories,
)
```

读查询也会涉及数据库事务语义，但这段读取会话会在等待模型前退出。它没有把事务一的行锁一直持有到模型回答。

`owned_turn()` 重新检查该 turn 与会话属于当前用户，并检查 `chat:read`。后续保存时使用 `write=True`，还会检查 `chat:send` 和行锁。

### 6.3 第三步：在线程中等待同步模型客户端

```python
async with asyncio.timeout(self.timeout_seconds):
    decision = await asyncio.to_thread(
        self.chat_agent.decide,
        request.message,
        history=history,
        memory_context=context,
        investigation_id=request.investigation_id,
        model_id=request.model_id,
    )
```

当前 `decide()` 使用同步 SDK，所以通过 `to_thread()` 避免在事件循环线程中直接等待同步网络请求。它仍是本次请求正在等待的工作，不是可靠后台任务。[Python 3.11 的线程与超时说明](https://docs.python.org/3.11/library/asyncio-task.html#asyncio.to_thread)

协程超时不保证工作线程和供应商请求已经停止。线程可能稍后取得响应；由于 `decide()` 不保存业务状态、不执行工具，晚到结果不会自行启动调查，但可能已经产生供应商用量。客户端自身超时与外层业务等待超时也不是同一个设置，当前 LLM 客户端超时为 120 秒、SDK 自动重试为 0；业务等待上限来自 `EntryServices.timeout_seconds`。

### 6.4 第四步：目标 Gate 与保存决定

如果是 `ToolInvocation`，程序调用 `validate_entry_target()`：明确绑定的目标必须一致；没有明确绑定时，目标必须作为独立 ID 出现在本次消息里，不能从历史记忆补造。

这项检查不证明用户有权操作，也不能完全证明自然语言真实意愿。它保护的是目标关联；授权和启动确认还在后面。

最后一个短事务重新取得该 turn，调用 `actions.save_decision()`，再返回 `actions.state()` 形成的视图。普通回答、可选记忆与决策记录一起提交，避免出现“回答成功但来源决策缺失”的半套记录。

## 7. `history_and_memories()`：两份材料究竟怎么来

代码：[history_and_memories()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/repository.py:94)。

### 7.1 查询条件：先选 10 条候选记录

```python
select(ChatTurnRow).where(
    ChatTurnRow.conversation_id == conversation_id,
    ChatTurnRow.actor_id == actor,
    ChatTurnRow.status == "SUCCEEDED",
    ChatTurnRow.action.in_(("CHAT", "AUTO", "ENTRY_DECISION")),
).order_by(ChatTurnRow.created_at.desc(), ChatTurnRow.turn_id).limit(10)
```

这里只读取同用户、同会话、已成功结算的记录。本次消息已保存为 `STARTED`，所以不进入这份历史；它会单独放入模型 payload 的 `message`。

重要细节：SQL 的 `limit(10)` 在后续 Python 过滤之前执行。假设候选中有 3 条工具动作，过滤后可能只留下 7 轮问答，不会继续向更早记录补足 10 轮。

### 7.2 校验响应，再排除不能作为普通历史的内容

每条记录先由 `read_response()` 验证响应摘要、turn、会话和成功状态对应关系。

| action | 处理方式 |
|---|---|
| `ENTRY_DECISION` | 必须有可靠决策记录；仅当决策为 `ChatDraft` 时进入普通历史 |
| `AUTO` | 读取并核验旧意图记录；仅 `CHAT`、`CLARIFY` 进入历史 |
| `CHAT` | 读取已有普通问答 |

旧 action 的读取是现有历史记录的读取路径，不意味着当前还会通过旧意图分类入口执行。工具结果被排除，是为了防止绕过案件权限：当前有权读某段聊天，不代表仍有权读历史案件结果。需要实时案件状态时，应再次调用进度工具。

### 7.3 `history` 是对话原文窗口

数据库先按时间降序查询，循环 `reversed(rows)` 后，从较早到较晚拼接：

```python
history.extend((
    {"role": "user", "content": row.user_text[:2000]},
    {"role": "assistant", "content": response.answer[:2000]},
))
```

例如：

```python
history = [
    {"role": "user", "content": "以后请用中文回答"},
    {"role": "assistant", "content": "好的，我会用中文回答。"},
    {"role": "user", "content": "解释一下 PSI"},
    {"role": "assistant", "content": "PSI 用于衡量分布差异……"},
]
```

每轮最多两条消息，单条截取前 2000 个字符。这个限制不是 token 数，也不是智能摘要：后面的否定、限制或关键对象可能被裁掉。数据库里的原文没有因此被改写，但本次模型看不到被裁掉的部分。

当前 `decide()` 会把这组 role/content 数据放进 JSON 的 `recent_history` 字段，而不是将它们展开成供应商 `messages` 中的多条历史消息。供应商真正收到的消息角色仍是 system 和 user 两条。

### 7.4 `memories` 是已保存的结构化记忆候选

若该 turn 有 `memory_payload`，函数反序列化为 `MemoryRecord`，并检查摘要、`source_id == turn_id`、`source_type == "CHAT_SESSION"`、scope 是否为当前会话。

这里的 `CHAT_SESSION` 是当前实现使用的来源类型标签。该路径的实际来源仍由 `source_id` 精确关联一个 turn，不能据此把它理解为整段会话的累计摘要。

```python
# 内容示意，完整 MemoryRecord 还包含来源及归属。
memory.content = {
    "user_preferences": ["使用中文解释，并给出代码入口"],
    "background": ["关注 credit-v3 的规则变更"],
    "follow_ups": [],
    "historical_findings": [],
    "execution_summaries": [],
}
```

`history` 按时间正序返回，供模型理解对话先后；`memories` 再反转为近期优先，供组装器先选择较近的内容。二者来自同一批近期候选，但承担不同用途：原文保留表达过程，结构化记忆突出值得延续的内容。

### 7.5 组装器只是选择，不在读取时重新提取记忆

[`assemble_context()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/memory/context.py:21) 校验 scope、重复来源，按候选顺序和分类顺序尝试放入完整正文条目。默认检查序列化对象长度是否不超过 `max_chars=12000`；不是按 token、语义相关度或模型摘要选取。预算不足时跳过整条正文，不截成半句话。

随后 `model_context()` 为所选记忆加上用途说明，再交给模型：它仅供参考，不是指令、权限或当前根因事实。

当前聊天链路还没有独立的长期累计召回、向量检索、完整 Prompt token 预算。这与调查工具结果使用离线 tokenizer 的预算是不同路径，不能把一个模块已接入 tokenizer 推导成整个聊天输入都已完成预算治理。

来源和摘要检查证明的是“这份记忆确实对应那条已保存记录”，不证明内容真实。组装器也不负责自动解决前后偏好冲突；近期优先选择不能等同于语义层面的事实纠错。

## 8. `ChatAgent.decide()`：从工具声明到模型响应

代码：[ChatAgent.decide()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/agent.py:26)。

### 8.1 先确定“提供哪些入口工具”，不是先调用一次分类模型

```python
declarations = entry_tool_declarations(tool_names)
offered_names = tuple(tool["function"]["name"] for tool in declarations)
```

[`entry_tool_declarations()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/tools.py:22) 检查名称是否已注册、是否重复，生成 OpenAI 兼容的工具声明。当前固定注册两个入口工具：

| 工具 | 参数 | 提出请求之后 |
|---|---|---|
| `get_investigation_progress` | `investigation_id` | 服务端鉴权后读取进度 |
| `start_investigation` | `model_id` | 等待显式确认，再鉴权启动 |

它们是案件业务入口，与调查图里的 `knowledge_search` 等任务工具分开。聊天模型不能通过这份声明直接访问任意调查任务工具。

当前 `EntryServices.prepare_entry()` 没有传入定制 `tool_names`，因此使用默认两项。**这里检查的是注册和本次提供列表，不是按 actor 完成所有业务权限筛选。** 用户能看到工具说明，也仍可能在执行前被拒绝。这一点不能描述成“发给模型的每个工具都已经最终授权”。

“解释 PSI”“是否应该调查”通常返回回答；“帮我查那个”通常追问；“查询 case-001 进度”可能提出进度工具；“启动 credit-v3 新调查”可能提出启动工具。意图体现在这次回答或原生工具调用中，没有再调用独立分类模型。

### 8.2 payload：模型需要理解的业务材料

```python
payload = {
    "message": message,
    "recent_history": history,
    "historical_memory": model_context(memory_context),
    "explicit_target": {
        "investigation_id": investigation_id,
        "model_id": model_id,
    },
}
```

`explicit_target` 表示调用方明确绑定的目标，可以为空；它不代表授权。这里没有把 `actor`、Bearer Token、数据库连接、授权函数或真实工具 handler 交给模型。

例如本次消息要求启动 `credit-v3`，同时请求明确绑定 `model_id="credit-v3"`，模型的工具参数必须与之相符。历史记忆曾提到另一个模型，不能作为替换本次目标的理由。

### 8.3 options：发给 SDK 的完整请求参数

```python
options = {
    "model": self.model,
    "messages": [
        {"role": "system", "content": ENTRY_DECISION_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ],
    "response_format": {"type": "json_object"},
}
if declarations:
    options.update(
        tools=declarations,
        tool_choice="auto",
        parallel_tool_calls=False,
    )
```

| 字段或语法 | 作用及边界 |
|---|---|
| `payload` | 被放入 user 消息中的业务数据 |
| `options` | SDK 请求的关键字参数集合 |
| `json.dumps(..., ensure_ascii=False)` | 把 Python 对象序列化为 JSON 文本，保留中文字符；不是加密或授权 |
| system Prompt | 规定回答、追问、工具调用边界；模型仍可能违反，需要程序校验 |
| `response_format` | 请求 JSON 对象输出；并不自动保证符合 `ChatDraft`，也不替代工具参数校验 |
| `tool_choice="auto"` | 允许模型回答或选择工具 |
| `parallel_tool_calls=False` | 请求不生成并行调用；本地解析器仍会检查只能有一个调用 |
| `strict=False` | 工具声明不依赖服务端 strict schema 保证，本地仍做完整验证 |
| `**options` | 将字典展开成 `create(model=..., messages=..., ...)` 形式 |

当前方法没有在此设置 `max_tokens`，也没有发送 SSE 流式事件；不要把供应商默认输出限制当作本方法显式配置的预算。

### 8.4 真正的模型调用只有这一行

```python
response = self.client.chat.completions.create(**options)
```

这是同步客户端发起远端 API 请求的位置。这里的 OpenAI 兼容客户端使用哪个平台，由 [`core/llm.py`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/core/llm.py) 中的客户端配置决定；类名不意味着请求必须发给 OpenAI。

### 8.5 `record_usage(response)` 记录用量，不记录回答

代码：[record_usage()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/core/observability.py:100)。

```python
record_usage(response)
return parse_entry_decision(response, offered_names=offered_names)
```

它读取供应商返回的 `response.usage`：`prompt_tokens` → input，`completion_tokens` → output，`total_tokens` → total，只接受非负整数。缺失项不会伪造为 0，也不会据此猜测费用。

这些用量更新到当前 Langfuse observation；观测未开启时不会形成有效用量记录，观测更新失败会降级，不应使聊天业务失败。它不保存 `response.message.content` 到聊天数据库，不生成记忆，也不执行工具。

调用顺序意味着：即使供应商响应随后因格式错误被拒绝，这次远端请求仍可能已经消耗 token，之前提取的 usage 仍有诊断意义。

`@observe_generation("entry-decision")` 为这次调用建立 generation 范围。成功解析后，它记录选中的注册工具名或 `NONE`，不把正文、工具参数或案件目标作为该元数据导出。实际回答的业务落库发生在 `actions.save_decision()` 和 `chat.complete_turn()`。

观测模块用 `ContextVar` 定位当前 observation，不把它存到 `app.state` 的“当前请求”字段。`to_thread()` 会传播当前上下文，因此正常调用中的 usage 可以关联到该调用的 generation；具体导出仍取决于 Langfuse 是否启用及导出是否成功。

### 8.6 本地解析器决定哪些输出可以接受

代码：[parse_entry_decision()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/tools.py:54)。

| 返回类型 | 要求 |
|---|---|
| `ChatDraft` | 正常 `stop`；正文为合法 JSON 对象；满足字段与长度约束；记忆不能冒充正式执行事实 |
| `ToolInvocation` | 正常 `tool_calls`；恰好一个原生 function call；名称在提供列表；参数满足对应模型；不能同时混合非空回答正文 |

解析器还拒绝旧 `function_call`、重复 JSON 字段、非法数字、多个选择、拒绝响应和截断结果。它不会从普通正文里搜一句“调用 start_investigation”然后执行。

“追问”与“普通回答”共用 `ChatDraft`，当前没有独立的追问类型或状态。两者在动作视图中都可能是 `ANSWERED`。

## 9. 返回值：内部决定、动作视图、最终响应是三件事

模型解析后的决定示意：

```python
ChatDraft(
    answer="请提供要查询的案件 ID。",
    memory=MemoryContent(),
)

ToolInvocation(
    call_id="call-demo-1",              # 供应商响应中的调用 ID
    name="start_investigation",
    arguments=StartInvestigationArguments(model_id="credit-v3"),
)
```

`ToolInvocation.call_id` 不是服务端的 `invocation_id`。后者按 turn 稳定生成，确认和执行依赖服务端保存的关联，不信任模型自己分配业务身份。

`prepare_entry()` 保存决定后，统一返回 [`EntryActionState`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/contracts.py:72)。以下是启动调查待确认视图的结构示意，ID 和摘要为占位说明：

```text
EntryActionState
  turn_id           = 服务端稳定 turn ID
  conversation_id   = 当前会话 ID
  status            = PENDING_CONFIRMATION
  decision          = ToolInvocation(...)
  decision_digest   = 保存决定的摘要
  invocation_id     = 服务端稳定调用 ID
  investigation_id  = 预分配案件 ID
  result            = None
```

有了预分配案件 ID，不代表案件已经创建。它让后续确认、执行、失败查询能够关联同一个业务对象。

| 模型决定或 Gate 结果 | 动作视图 status | `result` | 接下来的行为 |
|---|---|---|---|
| 普通回答／追问 | `ANSWERED` | 成功 `ChatResponse` | 直接返回 |
| 目标不一致，被 Gate 拒绝 | `REJECTED` | 成功 `ChatResponse`，answer 为追问／拒绝说明 | 不执行工具 |
| 进度工具 | `READY` | `None` | `EntryServices.chat()` 立即执行 |
| 启动工具 | `PENDING_CONFIRMATION` | `None` | 等待确认 |
| 决策阶段失败并成功登记失败结果 | `FAILED` | 失败 `ChatResponse` | 返回失败，不自动重调模型 |

外层 `EntryActionState.status` 描述动作生命周期；内层 `ChatResponse.status` 描述本次响应的成功或失败。因此 Gate 拒绝可以是外层 `REJECTED`、内层 `SUCCEEDED`：程序成功反馈“目标不明确，未执行”，不等于调查执行成功。

普通回答只有 `memory.item_count() > 0` 才生成会话记忆；空记忆不落一份空对象。记忆候选是这次模型和回答一起产生的，不是独立后台提取。启动建议与工具执行反馈在当前路径不会通过 `finish()` 再提取一份普通聊天记忆。

## 10. 确认与 `execute_entry()`：执行的是已保存决定

入口：[confirm_chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:137)、[execute_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:216)。

确认请求包含原调用的 `invocation_id`、`decision_digest` 和布尔值 `confirmed`，URL 指定原 `turn_id`。它没有新工具名、新模型目标或新 actor 字段，防止界面确认的 A 被请求替换为 B。确认结果重复提交可以复用，已确认事实不能改写。

```text
PENDING_CONFIRMATION
  ├─ confirmed=false → CANCELLED，保存取消响应
  └─ confirmed=true  → READY，确认事务提交后进入 execute_entry
```

这里需要调用确认接口。当前没有把用户另发一句“确认”自动绑定到待确认动作的逻辑。

`execute_entry()` 分三段：

| 阶段 | 内容 | 提交边界 |
|---|---|---|
| 执行前短事务 | 锁定 turn 和动作，重验归属、决定摘要、状态及业务权限，领取调用 | `STARTED` 提交后才执行 |
| 事务外执行 | 查询案件进度，或创建稳定案件并调用原 Runner | 不持续持有执行前的行锁 |
| 结果短事务 | 再验会话写权限，保存动作终态与 `ChatResponse` | 动作与最终响应一起提交 |

进度读取检查案件 READ。启动调查在执行前分别检查 create、READ、CONTINUE，不因为用户能发送聊天就允许操作任意模型。

进入实际业务前，如果动作已经是 `STARTED` 或终态，就返回当前视图；它不会看到 `STARTED` 就再执行一次。`PENDING_CONFIRMATION` 也不能直接执行。

查询走 `_progress()`；启动走 `_create_case()` → `_run_case()` → `InvestigationRunner.ainvoke()`，结束后再查询进度形成响应。当前请求仍等待 Runner 返回，`async def` 不会自动把它变成可靠后台任务。Runner 内部跨轮恢复与入口动作 `STARTED` 的自动接续是不同问题。

## 11. 数据库里分别保存什么

表定义：[conversations/models.py](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/models.py)。

| 表 | 主要权威内容 | 主要写入时机 |
|---|---|---|
| `chat_conversations` | 会话 ID 与 owner | 新会话第一次有效提交 |
| `chat_turns` | 用户原文、请求摘要、actor、turn 状态；结算后的响应及可选记忆 | 消息受理事务、最终结算事务 |
| `chat_entry_decisions` | 规范化原请求、模型决定和摘要、服务端调用与案件关联、确认事实、执行状态 | 决策、确认、领取、结算各短事务 |
| Investigation 账本及检查点 | 正式计划、阶段执行、Observation 与流程位置 | 原 Runner 执行期间 |

`app.state` 只引用操作这些数据的服务。返回 `EntryActionState` 时，`actions.state()` 从 turn 和动作记录组合视图，不是把某个全局 Python 字典当成持久化状态。

## 12. 并发与失败：逐个位置看会留下什么

### 12.1 正常并发保护的对象

`actions.begin()` 和 `claim_turn()` 使用 PostgreSQL 事务级 advisory lock：先固定请求键，再固定会话。相同请求的摘要检查和同会话新 turn 的创建不会盲目并行。数据库还以部分唯一索引约束一个会话最多有一条 `STARTED` turn。

模型等待期间事务锁已经释放；防止第二条消息进入，依赖已提交的活动 turn 和创建边界检查，不是依赖一直锁住数据库。`execute_entry()` 则通过动作行锁和 READY → STARTED 状态转换，阻止两个请求重复领取同一个动作。

这些是数据库级保护，适用于同进程异步任务、多个线程或多个 worker。`app.state` 本身不提供这类幂等保证。

一个容易忽略的产品行为：等待确认时，动作记录是 `PENDING_CONFIRMATION`，但 turn 仍然 `STARTED`。因此同会话新消息会被活动 turn 阻断，直至确认、取消或结算；当前不是允许任意交错的多 turn 聊天。

### 12.2 `prepare_entry()` 失败边界

| 失败位置 | 已保存内容 | 当前行为 |
|---|---|---|
| 请求参数非法 | 通常尚无业务写入 | 框架返回 422 |
| 身份无法认证 | 不进入业务方法 | 401；依赖未配置为 503 |
| 第一事务中的权限、会话归属或重复摘要冲突 | 事务内新增内容回滚 | 按错误映射为 403／404／409 |
| 读历史、校验记忆、模型网络、超时或解析失败 | 用户 turn 已可靠保存 | 尝试保存 `FAILED + DEPENDENCY`，说明没有执行入口工具 |
| 目标 Gate 不通过 | 原消息与合法结构的工具建议 | 保存 `REJECTED` 和反馈，不执行 |
| 模型结果已返回，但最终决策保存失败 | 可能只有 STARTED turn | 最后事务回滚；重试可能 409，不能声称决定已落库 |
| 进程退出或协程被取消 | 取决于最后成功提交位置 | 普通 `except Exception` 不覆盖所有中断；可能保留 DECIDING 状态 |

代码中的模型阶段 `try/except` 不包住最初 `actions.begin()`，也不包住末尾独立的保存决定事务，所以不能把所有错误都描述成“会自动保存失败响应”。失败登记本身若因数据库断连或权限撤销再次失败，也可能留下原 STARTED turn。

当前读取记忆时遇到摘要损坏会使本次准备失败，并没有自动丢弃坏记忆继续回答。当前 DECIDING 请求也没有超时租约或自动重领机制；重试时拒绝再次调用模型，仍需核实与恢复处理。

### 12.3 `execute_entry()` 失败边界

| 失败位置 | 当前行为 |
|---|---|
| 领取前无权限、未确认或记录不一致 | 尚不执行工具；返回相应错误 |
| STARTED 提交后发生已知业务错误 | 尝试以 FAILED 结算；例如权限、资源不可访问、明确冲突 |
| STARTED 后发生未知依赖异常 | 动作保存为 UNCERTAIN，内层响应为 FAILED；不表示外部业务一定没发生 |
| 进程退出、取消，或最终响应事务失败 | 可能仍保留 STARTED；按原 turn 查询，不自动重跑 |
| 结果已提交但 HTTP 响应丢失 | 使用原请求或查询接口取得已有结果，不重新执行 |

这里的不确定性是“业务效果是否已经发生尚未核实”。它不同于模型输出 `INCONCLUSIVE`：后者是证据或解释不足，两者不能互相替代。

### 12.4 HTTP 状态不等于调查结论

[`entry_response()`](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:46) 在没有最终 `result` 时默认返回 202，例如等待确认或正在执行；它并不证明任务已经交给可靠后台。

有最终响应时，成功为 200；FORBIDDEN／NOT_FOUND／CONFLICT 分别为 403／404／409；其他失败为 503。外围 `invoke()` 还把账本完整性错误映射为 500。Gate 拒绝但已成功反馈时，可能返回 HTTP 200 和外层 `REJECTED`，调用方要读业务状态。

客户端可以通过 `GET /chat/turns/{turn_id}` 查询状态。不要在超时后更换 request_id 重发“启动调查”：那会被当成新的逻辑动作，原调用的去重记录无法替新请求兜底。

## 13. 用三次请求串起完整流程

### 情况 A：普通咨询

```text
消息：“解释 PSI 是什么”
  → 保存 STARTED turn
  → 读取历史与记忆
  → decide 返回 ChatDraft
  → 决策、最终回答、可选非空记忆同事务提交
  → ANSWERED + ChatResponse(SUCCEEDED)，HTTP 200
```

### 情况 B：启动调查并确认

```text
消息：“请对 credit-v3 启动调查”，明确绑定 model_id=credit-v3
  → decide 返回 start_investigation 原生调用
  → 参数校验、目标 Gate
  → 保存 PENDING_CONFIRMATION，返回 HTTP 202

确认：原 turn_id + invocation_id + decision_digest + confirmed=true
  → 保存确认，READY
  → 检查业务权限、提交 STARTED
  → 创建稳定案件、运行原 InvestigationRunner
  → 查询最新轮次进度、保存最终响应
```

这不是“模型选了工具就已经执行”，也不是两次独立的意图分类。第二次请求提交的是用户确认；原先的模型决定不会因为确认而重新生成。

### 情况 C：模型返回了启动建议，但浏览器没收到

客户端沿用原完整请求重发：如果决定已经提交，返回原 PENDING_CONFIRMATION；如果只有用户消息已提交而决定未可靠保存，则返回冲突，不再调用模型。若确认后的工具已 STARTED，查询可以看到原调用和预分配案件关联，但当前入口不会自行把它恢复为“从未执行”。

因此前端应在发送前保留请求编号与完整请求体；收到响应后保存 conversation_id、turn_id；确认时使用服务端返回的调用编号和摘要。

## 14. 读代码时的核对顺序

| 顺序 | 入口 | 核对的问题 |
|---|---|---|
| 1 | [principal()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:16) | actor 从哪里来？ |
| 2 | [HTTP chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/api/routes/entrypoints.py:56) | 如何取得服务并交接参数？ |
| 3 | [EntryServices.chat()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:130) | 哪个状态会继续执行？ |
| 4 | [prepare_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:144) | 消息、模型调用、决定分别在什么事务边界？ |
| 5 | [actions.begin()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:67) | 请求重试怎样去重？ |
| 6 | [history_and_memories()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/repository.py:94) | 哪些历史会进入模型，哪些会被排除？ |
| 7 | [ChatAgent.decide()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/agent.py:26) | 工具、payload、options 如何组成请求？ |
| 8 | [parse_entry_decision()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/tools.py:54) | 哪些响应会被拒绝？ |
| 9 | [save_decision()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/conversations/action_repository.py:84) | 模型决定什么时候变成可靠记录？ |
| 10 | [execute_entry()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/entrypoints.py:216) | 何时开始真正的业务动作？ |
| 11 | [record_usage()](/C:/Users/Vinim/Desktop/RiskMind/backend/src/risk_platform/core/observability.py:100) | 用量去了哪里，为什么不是回答落库？ |

文档依据为上述当前代码，并对照 FastAPI 与 Python 官方说明核对框架语法。本次只编写说明，不修改业务实现、不调用真实模型。文中关于长程记忆、完整聊天 token 预算、可靠后台交接及入口中断自动接续的内容，均按当前未完成边界表述。
