"""调用外部 Embedding 模型，并校验、整理返回的向量。

本模块不在本地计算模型，也不执行切分、Milvus 写入或相似度搜索。
文档 Child 正文与用户问题使用同一个默认模型，映射到相同维度的向量空间。
"""

import os

from openai import AsyncOpenAI


DEFAULT_SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"  # OpenAI 兼容接口地址。
EMBEDDING_MODEL = "BAAI/bge-m3"  # 默认文档和问题共用此模型。
EMBEDDING_DIMENSION = 1024  # 每条向量的数值个数，需与 Milvus 向量字段维度一致。


class EmbeddingClient:
    """封装 Embedding 请求，确保输出与输入逐条对应且维度符合约定。"""

    def __init__(self, *, client: AsyncOpenAI, model: str):
        """保存上游客户端和模型名；构造对象时还不会发送 Embedding 请求。

        * 表示 client、model 必须按名称传参，也便于测试注入假客户端。
        """

        self.client = client
        self.model = model

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量转换文本，按原输入顺序返回向量；不接受部分成功结果。

        外层列表对应多段文本，内层列表是某段文本的 1024 维向量。
        请求或校验失败时向调用方抛出异常，不返回不完整向量列表。
        """

        # 空批次直接返回，不浪费上游请求；它与批次中包含空白文本是两种情况。
        if not texts:
            return []
        # strip() 仅用于检查，不会修改实际发送的原文；任意一条为空就拒绝整批。
        if any(not text.strip() for text in texts):
            raise ValueError("embedding input must not contain blank text")

        # SDK 将文本发送给外部模型；await 等待网络结果，向量并非在这里手工计算。
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts,
            encoding_format="float",  # 请求返回浮点数数组形式的向量。
        )
        # 少一条或多一条都无法可靠配对，不能默默忽略异常结果。
        if len(response.data) != len(texts):
            raise ValueError(
                "embedding response count does not match the input text count"
            )

        # 响应条目的排列顺序不直接作为输入顺序；以条目声明的 index 为准。
        vectors_by_index: dict[int, list[float]] = {}
        for item in response.data:
            # 属性缺失时得到 None，统一进入下面的非法下标处理。
            index = getattr(item, "index", None)
            if (
                not isinstance(index, int)
                or isinstance(index, bool)  # bool 是 int 的子类，需额外排除 True/False。
                or index < 0
                or index >= len(texts)
            ):
                raise ValueError("embedding response contains an invalid index")
            if index in vectors_by_index:  # 防止重复下标覆盖已经保存的另一条向量。
                raise ValueError("embedding response contains a duplicate index")
            vectors_by_index[index] = item.embedding

        # 例如输入三条文本，合法下标集合必须恰好是 {0, 1, 2}。
        expected_indices = set(range(len(texts)))
        if set(vectors_by_index) != expected_indices:
            raise ValueError("embedding response does not cover every input index")

        # 显式恢复输入顺序，保证调用方 zip(texts, vectors) 不会把向量配错正文。
        vectors = [vectors_by_index[index] for index in range(len(texts))]
        # 维度检查只检查长度；此处没有额外执行归一化或逐数值有限性检查。
        if any(len(vector) != EMBEDDING_DIMENSION for vector in vectors):
            raise ValueError(
                f"embedding response must contain {EMBEDDING_DIMENSION}-dimensional vectors"
            )

        return vectors

    async def embed_query(self, query: str) -> list[float]:
        """把一个问题包装成单元素批次，复用全部校验后取出唯一向量。"""

        return (await self.embed_texts([query]))[0]


# 进程内缓存客户端，复用 HTTP 连接池；缓存的不是文本或向量结果。
_CLIENT: EmbeddingClient | None = None

"""直连嵌入上游(硅基流动的 bge-m3,OpenAI 兼容)。
    进程内单例:复用同一 httpx 连接池,避免每次 embed 新建客户端累积连接/fd。"""
def get_embedding_client() -> EmbeddingClient:
    """首次读取环境配置并创建默认客户端，后续调用复用同一个对象。

    已创建客户端后修改环境变量，不会自动更新这个缓存对象中的配置。
    """

    global _CLIENT  # 后续赋值更新模块变量，而非创建同名局部变量。
    if _CLIENT is not None:
        return _CLIENT
    # 读取当前进程环境；本函数不会自行加载 .env 或 .env.example 文件。
    api_key = os.environ.get("SILICONFLOW_API_KEY")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY is not set")

    # AsyncOpenAI 是兼容协议客户端，不代表本次请求发送到 OpenAI 官网。
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
