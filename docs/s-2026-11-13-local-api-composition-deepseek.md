# 2026-11-13：DeepSeek 本地 API 依赖组装与聊天联调

## 实际范围

新增 bootstrap.create_local_app 显式工厂：单用户本地 Bearer 身份 → 白名单授权 → PostgreSQL Session / Checkpointer → 原 InvestigationRunner → 两个共享 DeepSeek 客户端的 Agent → 原 EntryServices。

默认 main.app 仍无业务依赖、默认拒绝；只有显式启用的本地工厂会安装依赖。不是生产登录系统、多租户权限服务或新的流程编排器。使用独立测试数据库和 127.0.0.1，不向公网部署本地 Token 模式。

## 数据与依赖归属

- LocalSettings 只保存本地身份及显式授予的业务模型范围，不进入 Agent 输入、State 或检查点。
- RISK_ALLOWED_MODEL_IDS 是业务模型 ID，例如 credit-v3；DEEPSEEK_MODEL 是供应商大模型名，二者不可混淆。
- 白名单授权覆盖该用户对指定业务模型的案件 CREATE/READ/CONTINUE 及知识检索，不是“只允许读取自己创建的案件”的多租户策略。
- 会话仍检查 owner；Bearer 请求的 actor 固定来自配置，不接受 X-Actor-ID 或正文覆盖。
- KnowledgeSearchTool 保存 PG 会话工厂、应用自有 Embedding/Milvus 客户端；查询范围固定到案件模型和已配置索引策略。
- lifespan 退出或部分启动失败时按反序关闭 Milvus、Embedding、Runner 专用锁引擎、Checkpointer、数据库引擎、共享模型客户端。Runner.aclose 只关闭它自己拥有的锁引擎。
- 本地工厂不自动执行 Alembic、checkpoint setup、知识索引构建或 serving route 激活。缺少关键数据库表时启动失败，不接受半初始化请求。

## 准备依赖

仓库根目录：

```powershell
python -m pip install -e "./backend[database,recovery,knowledge,dev]"
```

需要可访问的 PostgreSQL、Milvus，以及硅基流动 Embedding 凭证。DeepSeek 基础联调成功不代表这些依赖已经准备好。启动时构造 Milvus 客户端会建立连接，即使暂时只测普通聊天也需 Milvus 可访问；不要为了假装完整联调而用假检索服务替代真实部署。

当前进程已存在的供应商环境变量可沿用，缺少的项需配置：

```powershell
$env:DEEPSEEK_MODEL = "deepseek-flash"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
# DEEPSEEK_API_KEY 使用你已通过联调的官方密钥。
# SILICONFLOW_API_KEY 使用 Embedding 服务自己的密钥，不复用 DeepSeek 密钥。
$env:SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
$env:MILVUS_URI = "http://127.0.0.1:19530"
$env:RISK_DATABASE_URL = "postgresql+psycopg://用户名:密码@127.0.0.1:5432/数据库名"
```

含特殊字符的数据库密码需按 URL 规则编码。上述仅为模板，不能把占位密码当实际配置。本次开发不修改你的业务数据库。

## 显式准备数据库

核对 RISK_DATABASE_URL 指向你授权使用的数据库，再执行：

```powershell
python -m alembic -c backend/alembic.ini upgrade head
python scripts/setup_workflow_checkpoints.py --apply
```

第一条迁移业务表；第二条仅创建/升级 LangGraph checkpoint 表，要求 --apply 显式确认。普通启动不隐式建表。知识检索还需要已有的文档、索引和生效 serving route；这些由现有知识模块流程建立，不在本地 API 启动时自动生成。

## 配置本地身份及业务授权

```powershell
$env:RISK_LOCAL_MODE = "1"
$env:RISK_LOCAL_ACTOR_ID = "local-reviewer"
$env:RISK_ALLOWED_MODEL_IDS = '["credit-v3"]'
$env:RISK_KNOWLEDGE_PROFILE = "parent-child-v1"
# 为本地 HTTP 访问生成独立随机 Token；不会输出到终端。
$env:RISK_LOCAL_API_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Token 至少 32 字符，用于本地 API，不是 DeepSeek 密钥。客户端请求采用 Authorization: Bearer <本地Token>。若在第二个终端运行联调脚本，需以安全方式共享同一个本地 Token，例如当前 Windows 用户环境持久化后重开终端；不要在两个终端各生成一个不同值。

无效本地 Token 返回 401；案件不在业务模型白名单内返回权限错误。白名单为空、缺配置或未显式启用会拒绝启动。当前模型阶段/工具预算使用 120 秒，工具 top_k 上限 5；这不是整个案件的总超时或后台执行机制。

## 启动 API

```powershell
python scripts/serve_local.py
```

脚本固定监听 127.0.0.1:8000，并使用 Selector 事件循环。Windows 的 psycopg 异步连接不支持 Proactor；当前 Uvicorn 的默认非子进程循环可能选择 Proactor，因此不要直接照搬默认 Windows 启动命令。

高级部署可调用 risk_platform.bootstrap:create_local_app 工厂，但应自行保证事件循环、配置和关闭生命周期；生产应继续注入真实认证及授权提供方，而不是使用该本地身份配置。

## 真实聊天 HTTP 联调

保持 API 服务运行，在带有同一个 RISK_LOCAL_API_TOKEN 的另一个终端执行：

```powershell
python scripts/smoke_chat_http.py
```

只发送普通 PSI 问题，再按返回 turn_id 查询并比对保存结果。不会确认/启动调查，不访问真实客户信息；一次真实模型请求有供应商费用，会写入一个合成聊天 turn 及模型返回的非空记忆。

预期：

```text
chat_http_probe: PASS (answer persisted and read back; no case started)
```

基础供应商测试仍用 python scripts/smoke_llm.py；它不经过 HTTP/数据库。不要混淆两个脚本证明的范围。

## 调查链路验收

发送消息 → 模型提出 start_investigation → PENDING_CONFIRMATION → 用户提交 confirmation → execute_entry → _create_case → 原 Runner → 唯一图 prepare_plan/task_execution/observation/replanning → 最终响应。

确认前不创建案件。确认后仍等待调查返回；可靠后台 Outbox/Celery 交接尚未实现。知识工具失败必须形成明确失败/不充分结果，不能把缺少 serving route 视作有真实证据的成功调查。

真实调查联调还需验证模型的初始计划、工具请求、Observation 引用，以及知识库来源和生效索引；本次单独模型 smoke 通过不能代替这些验收。

## 测试与当前限制

自动测试使用真实 PostgreSQL、真实 Checkpointer、原 Runner，模型与检索用合成替身；因此证明组装、事务、权限和交接一致，不证明外部模型/Embedding/Milvus 已真实联通。

本机目前缺少 RISK_DATABASE_URL、本地认证配置及部分模型/检索配置，完整真实服务未在开发过程中启动。需按上述说明配置后运行 HTTP smoke，不把代码测试通过描述为实际业务部署完成。

审阅：[组合入口](../backend/src/risk_platform/bootstrap.py)、[资源生命周期测试](../backend/tests/test_bootstrap.py)、[真实 PG 组合测试](../backend/tests/workflow/test_bootstrap_postgres.py)、[HTTP 联调脚本](../scripts/smoke_chat_http.py)。

本阶段实际验证：默认测试 642 passed、121 skipped；真实 PostgreSQL 组合及入口回归 11 passed；本地资源/启动脚本测试 6 passed。真实 PG 验证使用随机隔离 schema，外部模型及检索为测试替身；存在一条既有 LangGraph 依赖弃用警告。临时数据库验证完成后停止，业务数据库未迁移，未提交 Git。
