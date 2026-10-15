"""Stable domain contracts for knowledge retrieval."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeDocumentType(StrEnum):
    MODEL_DEVELOPMENT_REPORT = "MODEL_DEVELOPMENT_REPORT"
    INCIDENT_REPORT = "INCIDENT_REPORT"


class RetrievalStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class KnowledgeSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)


class KnowledgeFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_types: list[KnowledgeDocumentType] = Field(default_factory=list)
    model_id: str | None = None
    case_id: str | None = None
    document_version: str | None = None


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    filters: KnowledgeFilters = Field(default_factory=KnowledgeFilters)
    top_k: int = Field(gt=0)


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: KnowledgeSourceRef
    document_type: KnowledgeDocumentType
    title: str = Field(min_length=1)
    section_path: str
    content: str = Field(min_length=1)
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
