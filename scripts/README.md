# 脚本目录

后续仅放可重复、可审计的初始化和本地演示脚本。演示数据必须为合成数据，不要包含真实客户信息。


## DeepSeek 官方模型联调

先在进程环境中配置 DEEPSEEK_API_KEY、DEEPSEEK_MODEL；DEEPSEEK_BASE_URL 默认为 https://api.deepseek.com。示例模型 ID 为 deepseek-flash，实际可用性及参数支持须实测。配置示例在根目录 .env.example，但 Python 不自动加载 .env，也不自动读取 Windows 用户注册表。重启 IDE/终端后才能继承新保存的用户环境变量。

安装 backend 后，从仓库根目录执行：

```powershell
python scripts/smoke_llm.py
```

脚本使用生产 ChatAgent 和解析器发起两次真实模型请求：JSON 回答、原生进度工具建议。后者不实际执行；不访问数据库、不创建案件、不启动调查。打印通过阶段；失败时输出结构化诊断：阶段、异常类型、HTTP 状态、受限格式的平台错误码、Retry-After 秒数或 UTC 时间。不输出密钥、请求/响应正文或原始异常。缺失或不安全的字段为 null，不推测等待时间。调用消耗真实供应商额度；SDK 重试关闭，超时为 120 秒。

### 组合现有 Agent

```python
from risk_platform.core.llm import create_llm_agents

chat_agent, investigation_agent = create_llm_agents()
# 将 investigation_agent 注入现有 InvestigationRunner；
# 将 chat_agent、Runner、数据库、工具工厂及真实授权策略注入 EntryServices。
# 全部请求结束后，组合层关闭共享客户端一次：chat_agent.client.close()
```

工厂仅创建两个 Agent，共享一个 OpenAI-compatible 客户端及明确模型 ID，不做模型请求。未配置模型 ID 或密钥会明确失败，不回退智谱或 ModelScope 配置。旧 create_zhipu_client/get_zhipu_model 及旧联调脚本已退役。

当前默认 main.app 仍不自动注入 EntryServices 或认证。该变更不等于完整 HTTP 业务组装；普通模型联调成功也不证明调查阶段已完成真实联调。


### 限额类失败诊断

失败阶段为 configuration（配置/构建）、structured_answer（JSON 回答）、native_tool_call（原生工具请求）或 client_cleanup（关闭客户端）。示例：

```text
model_probe: FAIL {"stage":"structured_answer","exception_type":"RateLimitError","http_status":429,"provider_error_code":"insufficient_quota","retry_after_seconds":null,"retry_after_utc":null}
```

平台错误码由响应提供，示例不保证 各平台使用相同名称。429 本身不能区分额度、频率或容量问题。Retry-After 若存在可供人工安排下次尝试；字段为 null 表示未提供或无法安全解析，不表示立即可重试。脚本不会自动重试。


当前使用 DeepSeek 官方 API Key，不使用智谱密钥或 ModelScope Token。官方示例：[首次调用](https://api-docs.deepseek.com/zh-cn/)。

一次设置当前终端和 Windows 用户环境（密钥不写入命令历史）：

```powershell
$secret = Read-Host "输入 DeepSeek 官方 API Key" -AsSecureString
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", $secret).Password
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-flash"
foreach ($name in @("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")) {
    [Environment]::SetEnvironmentVariable($name, [Environment]::GetEnvironmentVariable($name, "Process"), "User")
}
Remove-Variable secret
python scripts/smoke_llm.py
```

当前终端立即生效；其他 IDE/终端需重启。切换服务不保证消除 429，仍需用脚本诊断实际返回。


### DeepSeek 参数边界

当前官方示例模型为 deepseek-flash，模型必须通过 DEEPSEEK_MODEL 明确设置，不静默回退。文档核对日期：2026-11-13。当前使用标准 Chat Completions 地址 https://api.deepseek.com，入口工具 strict=false（与调查工具保持一致），完整白名单、JSON、参数模型及目标校验仍由本地执行。DeepSeek strict=true 属于 /beta 功能，本次不启用。

JSON 模式保留 response_format=json_object，提示词包含 JSON 样例；空内容或截断仍明确失败，不偷偷重试。官方资料：[JSON 输出](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)、[工具调用](https://api-docs.deepseek.com/zh-cn/guides/tool_calls/)。

客户端 120 秒超时不改变业务入口自己的超时预算；真实联调需要本机配置 DeepSeek 密钥。模型调用有实际费用，以账号的官方价格和余额为准。


## 完整本地聊天 API

配置数据库、知识服务、本地 Bearer 身份与业务模型白名单后：

```powershell
python scripts/serve_local.py
# 另一个使用相同本地 Token 的终端：
python scripts/smoke_chat_http.py
```

完整配置、显式数据库初始化和 Windows 事件循环说明见 [本地应用组装指南](../docs/s-2026-11-13-local-api-composition-deepseek.md)。默认 main.app 继续拒绝未配置请求；本地工厂不是生产认证方案。
