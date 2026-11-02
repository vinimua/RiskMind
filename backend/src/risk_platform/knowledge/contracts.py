"""知识检索的数据契约：定义请求、来源、命中和结果的结构与校验规则。

这里只描述数据，不执行分块、向量化或数据库查询。

KnowledgeSearchRequest       检索请求
  └─ KnowledgeFilters        搜索范围

KnowledgeRetrievalResult     检索结果
  └─ KnowledgeHit[]          命中的知识片段
       └─ KnowledgeSourceRef 来源标识
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _validate_non_blank(value: str) -> str:
    # 仅拒绝空白内容，保留原始字符串，不自动去除两端空格。
    if not value.strip():
        raise ValueError("字段值不能为空或仅包含空白字符")
    return value


# 在 Pydantic 完成字符串类型处理后，统一执行非空白校验。
_NonBlankString = Annotated[str, AfterValidator(_validate_non_blank)]


class KnowledgeDocumentType(StrEnum):
    """当前支持的知识文档类型。"""

    MODEL_DEVELOPMENT_REPORT = "MODEL_DEVELOPMENT_REPORT"
    INCIDENT_REPORT = "INCIDENT_REPORT"


class RetrievalStatus(StrEnum):
    """检索执行状态；执行成功但没有命中，仍属于 SUCCEEDED。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"

class KnowledgeFilters(BaseModel):
    """检索范围；空类型列表和 None 表示不对对应字段增加筛选条件。"""

    model_config = ConfigDict(extra="forbid")

    # default_factory 为每个实例创建独立的默认列表。
    document_types: list[KnowledgeDocumentType] = Field(default_factory=list)
    document_id: _NonBlankString | None = None
    model_id: _NonBlankString | None = None
    document_version: _NonBlankString | None = None


class KnowledgeSearchRequest(BaseModel):
    """一次检索请求：查询内容、筛选条件和最多返回的命中数量。"""

    model_config = ConfigDict(extra="forbid")

    query: _NonBlankString
    filters: KnowledgeFilters = Field(default_factory=KnowledgeFilters)
    top_k: int = Field(gt=0)


class KnowledgeSourceRef(BaseModel):
    """通过文档、版本和片段定位来源；异常报告的 document_id 使用 Case ID。"""

    # 拒绝未定义字段，避免调用方拼错字段名后被静默忽略；其他模型同样适用。
    model_config = ConfigDict(extra="forbid")

    document_id: _NonBlankString
    document_version: _NonBlankString
    chunk_id: _NonBlankString


class KnowledgeHit(BaseModel):
    """带来源的知识片段，用于提供调查线索，不直接等同于业务 Evidence。"""

    model_config = ConfigDict(extra="forbid")

    source: KnowledgeSourceRef
    document_type: KnowledgeDocumentType
    model_id: _NonBlankString | None = None
    title: _NonBlankString
    section_path: str  # 章节路径；无章节时允许为空字符串。
    content: _NonBlankString
    rank: int = Field(gt=0)  # 从 1 开始的结果名次，不是相似度分数。


class KnowledgeRetrievalResult(BaseModel):
    """通用检索结果；Dense 检索的 snapshot 和 score 由 retrieval.py 单独建模。"""

    model_config = ConfigDict(extra="forbid")

    status: RetrievalStatus
    hits: list[KnowledgeHit] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        # 字段校验完成后，再检查状态、错误和命中之间的约束。
        if self.status is RetrievalStatus.FAILED:
            # 失败必须说明原因，且不能携带部分命中，避免被当作正常结果使用。
            if not self.error or not self.error.strip():
                raise ValueError("检索失败时必须提供非空的错误信息")
            if self.hits:
                raise ValueError("检索失败时不能包含命中结果")
            return self

        # 成功允许零命中，但不能同时携带错误。
        if self.error is not None:
            raise ValueError("检索成功时不能包含错误信息")

        # 命中须按 1..N 连续排列，不能跳号或乱序。
        expected_ranks = list(range(1, len(self.hits) + 1))
        actual_ranks = [hit.rank for hit in self.hits]
        if actual_ranks != expected_ranks:
            raise ValueError("命中结果必须按从 1 开始的连续排名排列")

        # 同一次结果中，同一个知识片段只能出现一次。
        chunk_ids = [hit.source.chunk_id for hit in self.hits]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("检索结果不能包含重复的知识片段")
        return self
