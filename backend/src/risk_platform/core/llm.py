"""DeepSeek official OpenAI-compatible client and the two existing Agent bindings.

Read process environment only; no registry access, dotenv loading or implicit
fallback to another provider. The composition caller owns the shared client.
"""
import os
from openai import OpenAI
from langchain_openai import ChatOpenAI

DEFAULT_BASE_URL = "https://api.deepseek.com"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def create_llm_client() -> OpenAI:
    """No SDK retries: business retries belong to explicit execution policy."""
    return OpenAI(
        api_key=_required("DEEPSEEK_API_KEY"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        timeout=120.0,
        max_retries=0,
    )


def get_llm_model() -> str:
    """Require the full provider model ID; never silently change the model."""
    return _required("DEEPSEEK_MODEL")


def create_llm_agents():
    """Bind ChatAgent and InvestigationAgent to one configured client/model.

    调用方关闭 investigator.client 和 tool_model.root_async_client；
    LangChain 与普通 JSON 调用共享同步客户端，不创建第二套认证配置。
    """
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.agents.investigation import InvestigationAgent

    model = get_llm_model()
    client = create_llm_client()
    try:
        tool_model = ChatOpenAI(model=model, api_key=client.api_key, base_url=str(client.base_url),
            root_client=client, client=client.chat.completions, timeout=120.0, max_retries=0)
        return ChatAgent(model, tool_model=tool_model), InvestigationAgent(client, model, tool_model=tool_model)
    except Exception:
        client.close()
        raise
