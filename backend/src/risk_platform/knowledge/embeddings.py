"""SiliconFlow embedding access for the knowledge module."""

import os

from openai import OpenAI


DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "BAAI/bge-m3"
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
        if len(response.data) != len(texts):
            raise ValueError(
                "embedding response count does not match the input text count"
            )

        vectors_by_index: dict[int, list[float]] = {}
        for item in response.data:
            index = getattr(item, "index", None)
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(texts)
            ):
                raise ValueError("embedding response contains an invalid index")
            if index in vectors_by_index:
                raise ValueError("embedding response contains a duplicate index")
            vectors_by_index[index] = item.embedding

        expected_indices = set(range(len(texts)))
        if set(vectors_by_index) != expected_indices:
            raise ValueError("embedding response does not cover every input index")

        vectors = [vectors_by_index[index] for index in range(len(texts))]
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
    return EmbeddingClient(client=client, model=EMBEDDING_MODEL)
