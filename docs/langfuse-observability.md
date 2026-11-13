> 当前聊天链路只产生一次 `entry-decision` generation；旧意图分类和独立回答调用已删除。入口状态从 EntryActionState 记录，确认执行与调查内部详细 spans 尚未完整接入。

# 聊天入口决策与 Langfuse 观测

## 当前接入范围

聊天请求由 EntryServices.chat 的 trace_chat 装饰器创建根 observation；ChatAgent.decide 创建 entry-decision generation。asyncio.to_thread 继承上下文，因此模型调用属于本次请求 trace。SDK 记录开始结束时间，可在平台查看耗时。

generation 仅记录 selected_tool（注册工具名或 NONE），不额外生成意图标签或 confidence，不导出工具参数。

root metadata 包含哈希生成的 conversation_id / turn_id、action、cached、请求 status / error_code。重发复用已有结果会建立新的请求 trace 并标记 cached，不重新调用模型。

供应商返回的 prompt_tokens / completion_tokens / total_tokens 分别映射 input / output / total。没有返回时不填假数据。费用依赖 Langfuse 模型价格配置，尤其是自定义或兼容供应商模型；本次没有预算阻断、费用报表或对账功能。

## 配置和启动

安装可选依赖：
```powershell
python -m pip install -e "./backend[observability]"
```

由部署环境提供（.env.example 仅是模板，当前代码不自动加载 .env）：
```text
RISK_LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=<项目公钥>
LANGFUSE_SECRET_KEY=<项目私钥>
LANGFUSE_BASE_URL=<实际 Langfuse 服务地址>
```

缺少配置、未安装 SDK、SDK 初始化/更新/关闭失败均不阻断业务。不要将正式密钥写入仓库。默认关闭，不会自动开通云账号、部署服务或上传现有历史。

本项目可选依赖范围为 Langfuse Python SDK >=4,<5，采用 observations-first API；4.18.0 已通过离线真实 SDK 验证。FastAPI lifespan 退出时在线程中调用 SDK shutdown；直接调用应用服务的脚本应在退出前主动调用 shutdown_observability。

## 数据边界与降级

只传结构化属性、模型名称和实际用量，不传用户正文、模型答案、记忆、Prompt、工具结果、actor_id、目标模型/案件 ID、凭证或原始异常文本。不会自动安装 LangChain/OpenAI 全量回调。系统生成的关联 ID 仍属于可关联运行标识，应按部署的数据管理要求处理。

SDK 上下文退出不接收原始异常，防止自动导出异常内容。业务错误按 operation_failed 记录 ERROR，原业务异常和取消继续抛出；观测异常只记录固定警告，不附带 SDK 原始异常与 traceback。回答业务失败但没有抛异常时，根观测根据入口状态和最终响应标记错误。

观测不替代 chat_turns、执行账本或权限审计；数据库写入失败不能被此降级机制忽略。

## 阅读顺序与边界

1. core/observability.py：配置、生命周期、异常隔离、装饰器、token 用量及工具选择。
2. conversations/agent.py：入口决策 generation 与用量记录。
3. entrypoints.py：整次聊天请求及入口状态。
4. main.py：应用关闭时清理 SDK。

本次覆盖聊天请求、单次入口决策。告警入口、调查图内规划、查询生成、知识工具、Observation 各阶段尚未逐项接入 generation/span；确认接口执行调查尚未建立独立根 trace，不能据此声称已记录调查内所有模型费用。

单元测试覆盖关闭状态、SDK 异常、错误脱敏、业务异常/取消保留、线程上下文、用量和生命周期。可选 test_observability_sdk.py 使用真实 SDK + 内存 exporter + HTTP mock，不连接云服务。真实 Langfuse 服务的接收、界面筛选和价格配置尚需部署后验证。

SDK API 依据：[Langfuse Python v4 迁移说明](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4)。
