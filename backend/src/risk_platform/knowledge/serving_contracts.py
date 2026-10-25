"""Serving contracts only; dense search and context resolution are separate stages."""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.contracts import KnowledgeHit, _NonBlankString
from risk_platform.knowledge.indexing.profiles import IndexProfile


class RetrievalRouteSnapshot(BaseModel):
    """Resolve once at request start; never re-resolve within that request."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    index_profile: IndexProfile
    collection: _NonBlankString
    route_version: int = Field(gt=0, strict=True)


class CollectionBuildAcceptance(BaseModel):
    """Immutable successful acceptance, independent of ingestion READY."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    build_id: _NonBlankString
    index_profile: IndexProfile
    collection: _NonBlankString
    source_chunk_count: int = Field(ge=0, strict=True)
    build_complete: Literal[True] = True
    validation_status: Literal["PASSED"] = "PASSED"
    authoritative_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: AwareDatetime
    validated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_acceptance(self) -> Self:
        if self.validation_digest != self.authoritative_snapshot_digest:
            raise ValueError("validation must match the authoritative source snapshot")
        if self.validated_at < self.completed_at:
            raise ValueError("validation cannot precede build completion")
        return self


class KnowledgeContext(BaseModel):
    """Knowledge context is not evidence; child hits retain provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    context_id: _NonBlankString
    content: _NonBlankString
    supporting_hits: tuple[KnowledgeHit, ...] = Field(min_length=1)
