from types import SimpleNamespace

import pytest

from risk_platform.knowledge import embeddings


def _vector(dimension: int = embeddings.EMBEDDING_DIMENSION) -> list[float]:
    return [0.1] * dimension


class FakeEmbeddings:
    def __init__(self, vectors: list[list[float]]):
        self.vectors = vectors
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(embedding=vector) for vector in self.vectors]
        )


class FakeOpenAIClient:
    def __init__(self, vectors: list[list[float]]):
        self.embeddings = FakeEmbeddings(vectors)


def test_create_embedding_client_uses_environment(monkeypatch) -> None:
    received: dict[str, str] = {}

    def fake_openai(*, api_key: str, base_url: str):
        received.update(api_key=api_key, base_url=base_url)
        return FakeOpenAIClient([])

    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    monkeypatch.setenv("SILICONFLOW_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("SILICONFLOW_EMBEDDING_MODEL", "example/embedding-model")
    monkeypatch.setattr(embeddings, "OpenAI", fake_openai)

    embedding_client = embeddings.create_embedding_client()

    assert received == {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
    }
    assert embedding_client.model == "example/embedding-model"


def test_create_embedding_client_uses_frozen_defaults(monkeypatch) -> None:
    received: dict[str, str] = {}

    def fake_openai(*, api_key: str, base_url: str):
        received.update(api_key=api_key, base_url=base_url)
        return FakeOpenAIClient([])

    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-key")
    monkeypatch.delenv("SILICONFLOW_BASE_URL", raising=False)
    monkeypatch.delenv("SILICONFLOW_EMBEDDING_MODEL", raising=False)
    monkeypatch.setattr(embeddings, "OpenAI", fake_openai)

    embedding_client = embeddings.create_embedding_client()

    assert received["base_url"] == embeddings.DEFAULT_SILICONFLOW_BASE_URL
    assert embedding_client.model == embeddings.DEFAULT_SILICONFLOW_EMBEDDING_MODEL
    assert embeddings.EMBEDDING_DIMENSION == 1024


def test_create_embedding_client_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SILICONFLOW_API_KEY is not set"):
        embeddings.create_embedding_client()


def test_embed_texts_calls_api_and_preserves_response_order() -> None:
    first_vector = _vector()
    second_vector = _vector()
    second_vector[0] = 0.2
    client = FakeOpenAIClient([first_vector, second_vector])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    vectors = embedding_client.embed_texts(["第一个片段", "第二个片段"])

    assert vectors == [first_vector, second_vector]
    assert client.embeddings.calls == [
        {
            "model": "BAAI/bge-m3",
            "input": ["第一个片段", "第二个片段"],
            "encoding_format": "float",
        }
    ]


def test_embed_query_returns_the_only_vector() -> None:
    vector = _vector()
    client = FakeOpenAIClient([vector])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    assert embedding_client.embed_query("credit_v3 KS 下降") == vector


def test_embed_texts_returns_empty_without_calling_api() -> None:
    client = FakeOpenAIClient([])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    assert embedding_client.embed_texts([]) == []
    assert client.embeddings.calls == []


@pytest.mark.parametrize("text", ["", "   "])
def test_embed_texts_rejects_blank_text(text: str) -> None:
    client = FakeOpenAIClient([])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    with pytest.raises(ValueError, match="must not contain blank text"):
        embedding_client.embed_texts([text])

    assert client.embeddings.calls == []


def test_embed_texts_rejects_an_unexpected_vector_dimension() -> None:
    client = FakeOpenAIClient([_vector(dimension=3)])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    with pytest.raises(ValueError, match="1024-dimensional"):
        embedding_client.embed_texts(["模型开发报告"])


def test_embed_texts_rejects_a_response_count_mismatch() -> None:
    client = FakeOpenAIClient([_vector()])
    embedding_client = embeddings.EmbeddingClient(
        client=client,
        model="BAAI/bge-m3",
    )

    with pytest.raises(ValueError, match="response count"):
        embedding_client.embed_texts(["第一个片段", "第二个片段"])
