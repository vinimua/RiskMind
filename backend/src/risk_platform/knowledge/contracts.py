"""Stable domain contracts for knowledge retrieval."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _validate_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value


_NonBlankString = Annotated[str, AfterValidator(_validate_non_blank)]


class KnowledgeDocumentType(StrEnum):
    MODEL_DEVELOPMENT_REPORT = "MODEL_DEVELOPMENT_REPORT"
    INCIDENT_REPORT = "INCIDENT_REPORT"


class RetrievalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class KnowledgeSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: _NonBlankString
    document_version: _NonBlankString
    chunk_id: _NonBlankString


class KnowledgeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_types: list[KnowledgeDocumentType] = Field(default_factory=list)
    model_id: _NonBlankString | None = None
    case_id: _NonBlankString | None = None
    document_version: _NonBlankString | None = None


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: _NonBlankString
    filters: KnowledgeFilters = Field(default_factory=KnowledgeFilters)
    top_k: int = Field(gt=0)


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: KnowledgeSourceRef
    document_type: KnowledgeDocumentType
    model_id: _NonBlankString | None = None
    case_id: _NonBlankString | None = None
    title: _NonBlankString
    section_path: str
    content: _NonBlankString
    rank: int = Field(gt=0)


class KnowledgeRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RetrievalStatus
    hits: list[KnowledgeHit] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is RetrievalStatus.FAILED:
            if not self.error or not self.error.strip():
                raise ValueError("failed knowledge retrieval requires an error")
            if self.hits:
                raise ValueError("failed knowledge retrieval cannot contain hits")
            return self

        if self.error is not None:
            raise ValueError("successful knowledge retrieval cannot contain an error")

        expected_ranks = list(range(1, len(self.hits) + 1))
        actual_ranks = [hit.rank for hit in self.hits]
        if actual_ranks != expected_ranks:
            raise ValueError("knowledge hits must be ordered with contiguous ranks")

        chunk_ids = [hit.source.chunk_id for hit in self.hits]
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("knowledge retrieval cannot contain duplicate chunks")
        return self
