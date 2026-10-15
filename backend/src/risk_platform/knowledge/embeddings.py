"""SiliconFlow embedding access for the knowledge module."""

import os

from openai import OpenAI


DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_SILICONFLOW_EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMENSION = 1024


class EmbeddingClient:
    """Create and validate embeddings used by the Milvus read model."""

    def __init__(self, *, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("embedding input must not contain blank text")

        response = self.client.embeddings.create(
            model=self.model,
            input=texts,
            encoding_format="float",
        )
        vectors = [item.embedding for item in response.data]

        if len(vectors) != len(texts):
            raise ValueError(
                "embedding response count does not match the input text count"
            )
        if any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
            raise ValueError(
                f"embedding response must contain {EMBEDDING_DIMENSION}-dimensional vectors"
            )

        return vectors

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]


def create_embedding_client() -> EmbeddingClient:
    """Create the configured SiliconFlow embedding client."""

    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is not set")

    client = OpenAI(
        api_key=api_key,
        base_url=os.environ.get(
            "SILICONFLOW_BASE_URL",
            DEFAULT_SILICONFLOW_BASE_URL,
        ),
    )
    model = os.environ.get(
        "SILICONFLOW_EMBEDDING_MODEL",
        DEFAULT_SILICONFLOW_EMBEDDING_MODEL,
    )
    return EmbeddingClient(client=client, model=model)
