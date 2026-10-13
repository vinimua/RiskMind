"""Construction and configuration of the Zhipu OpenAI-compatible client."""

import os

from openai import OpenAI


DEFAULT_ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_ZHIPU_MODEL = "glm-4.7-flash"


def create_zhipu_client() -> OpenAI:
    """Create a Zhipu client from the process environment."""

    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("ZHIPU_API_KEY is not set")

    return OpenAI(
        api_key=api_key,
        base_url=os.environ.get("ZHIPU_BASE_URL", DEFAULT_ZHIPU_BASE_URL),
    )


def get_zhipu_model() -> str:
    """Return the configured Zhipu model name."""

    return os.environ.get("ZHIPU_MODEL", DEFAULT_ZHIPU_MODEL)
