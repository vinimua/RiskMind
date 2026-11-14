"""DeepSeek official OpenAI-compatible client and the two existing Agent bindings.

Read process environment only; no registry access, dotenv loading or implicit
fallback to another provider. The composition caller owns the shared client.
"""
import os
from openai import OpenAI

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

    Returns (chat_agent, investigation_agent). The caller closes the shared
    client once, after all work finishes. This does not configure auth or Runner.
    """
    from risk_platform.conversations.agent import ChatAgent
    from risk_platform.agents.investigation import InvestigationAgent

    model = get_llm_model()
    client = create_llm_client()
    return ChatAgent(client, model), InvestigationAgent(client, model)
