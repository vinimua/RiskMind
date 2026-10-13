import pytest

from risk_platform.core import llm


def test_create_zhipu_client_uses_environment(monkeypatch) -> None:
    received: dict[str, str] = {}

    def fake_openai(*, api_key: str, base_url: str):
        received.update(api_key=api_key, base_url=base_url)
        return object()

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    monkeypatch.setenv("ZHIPU_BASE_URL", "https://example.test/v1/")
    monkeypatch.setattr(llm, "OpenAI", fake_openai)

    client = llm.create_zhipu_client()

    assert client is not None
    assert received == {
        "api_key": "test-key",
        "base_url": "https://example.test/v1/",
    }


def test_create_zhipu_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ZHIPU_API_KEY is not set"):
        llm.create_zhipu_client()


def test_zhipu_defaults(monkeypatch) -> None:
    received: dict[str, str] = {}

    def fake_openai(*, api_key: str, base_url: str):
        received.update(api_key=api_key, base_url=base_url)
        return object()

    monkeypatch.setenv("ZHIPU_API_KEY", "test-key")
    monkeypatch.delenv("ZHIPU_BASE_URL", raising=False)
    monkeypatch.delenv("ZHIPU_MODEL", raising=False)
    monkeypatch.setattr(llm, "OpenAI", fake_openai)

    llm.create_zhipu_client()

    assert received["base_url"] == llm.DEFAULT_ZHIPU_BASE_URL
    assert llm.get_zhipu_model() == llm.DEFAULT_ZHIPU_MODEL


def test_get_zhipu_model_uses_environment(monkeypatch) -> None:
    monkeypatch.setenv("ZHIPU_MODEL", "custom-model")

    assert llm.get_zhipu_model() == "custom-model"
