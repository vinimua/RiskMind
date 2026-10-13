"""Verify Zhipu JSON mode through the OpenAI-compatible API."""

import os
import sys
from dataclasses import dataclass

from pydantic import BaseModel

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4.7-flash"


class SmokePlan(BaseModel):
    goal: str
    tasks: list[str]


@dataclass(frozen=True)
class SmokeConfig:
    api_key: str
    base_url: str
    model: str


def load_config() -> SmokeConfig:
    api_key = os.environ.get("ZHIPU_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ZHIPU_API_KEY is not set")

    return SmokeConfig(
        api_key=api_key,
        base_url=os.environ.get("ZHIPU_BASE_URL", DEFAULT_BASE_URL).strip()
        or DEFAULT_BASE_URL,
        model=os.environ.get("ZHIPU_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
    )


def run_smoke_test(config: SmokeConfig) -> SmokePlan:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        temperature=0,
    )
    json_model = model.bind(response_format={"type": "json_object"})
    response = json_model.invoke(
        [
            {
                "role": "system",
                "content": (
                    "只返回一个 JSON 对象，不要输出 Markdown 或额外文字。"
                    "JSON 必须严格使用以下结构："
                    '{"goal": "字符串", "tasks": ["字符串", "字符串"]}。'
                    "goal 和 tasks 都必须存在，tasks 至少包含一项。"
                ),
            },
            {
                "role": "user",
                "content": "为什么 credit_v3 最近 KS 下降？",
            },
        ]
    )

    content = response.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Zhipu returned an empty message content")

    return SmokePlan.model_validate_json(content)


def main() -> int:
    try:
        plan = run_smoke_test(load_config())
    except Exception as exc:
        print(f"Zhipu structured output smoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(plan.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
