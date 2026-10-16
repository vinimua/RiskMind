
import os

from openai import AsyncOpenAI


DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_DIMENSION = 1024


class EmbeddingClient:
    """Create and validate embeddings used by the Milvus read model."""

    def __init__(self, *, client: AsyncOpenAI, model: str):
        self.client = client
        self.model = model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("embedding input must not contain blank text")

        response = await self.client.embeddings.create(
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

    async def embed_query(self, query: str) -> list[float]:
        return (await self.embed_texts([query]))[0]


_CLIENT: EmbeddingClient | None = None

"""直连嵌入上游(硅基流动的 bge-m3,OpenAI 兼容)。
    进程内单例:复用同一 httpx 连接池,避免每次 embed 新建客户端累积连接/fd。"""
def get_embedding_client() -> EmbeddingClient:

    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is not set")

    _CLIENT = EmbeddingClient(
        client=AsyncOpenAI(
            api_key=api_key,
            base_url=os.environ.get(
                "SILICONFLOW_BASE_URL",
                DEFAULT_SILICONFLOW_BASE_URL,
            ),
        ),
        model=EMBEDDING_MODEL,
    )
    return _CLIENT
