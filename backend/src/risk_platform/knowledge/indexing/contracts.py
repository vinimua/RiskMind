"""Minimal contracts for preparing searchable knowledge without persistence."""

from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.chunking import (
    KnowledgeChunk, KnowledgeDocument, _NonBlankString, _StableSourceUri,
)
from risk_platform.knowledge.contracts import KnowledgeDocumentType


class KnowledgeContextUnit(BaseModel):
    """Original continuous context, independent of overlapping search chunks."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_id: _NonBlankString
    document_id: _NonBlankString
    document_version: _NonBlankString
    document_type: KnowledgeDocumentType
    title: _NonBlankString
    section_path: str
    content: _NonBlankString
    model_id: _NonBlankString | None = None
    source_uri: _StableSourceUri
    page_start: int | None = Field(default=None, gt=0)
    page_end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_page_span(self) -> Self:
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("context page range requires both endpoints")
        if self.page_start is not None and self.page_start > self.page_end:
            raise ValueError("context page_start cannot exceed page_end")
        return self


class KnowledgeContextLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: _NonBlankString
    context_id: _NonBlankString
    child_ordinal: int = Field(ge=0, strict=True)


class PreparedKnowledge(BaseModel):
    """Ordered search chunks produced from one knowledge document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_chunks: tuple[KnowledgeChunk, ...]
    context_units: tuple[KnowledgeContextUnit, ...] = ()
    context_links: tuple[KnowledgeContextLink, ...] = ()

    @model_validator(mode="after")
    def validate_context_mapping(self) -> Self:
        # Baseline has no context mapping and retains its original contract.
        if not self.context_units and not self.context_links:
            return self
        chunks = {chunk.chunk_id: chunk for chunk in self.search_chunks}
        contexts = {unit.context_id: unit for unit in self.context_units}
        if len(chunks) != len(self.search_chunks) or len(contexts) != len(self.context_units):
            raise ValueError("child and context IDs must be unique")
        linked: set[str] = set()
        ordinals: dict[str, list[int]] = {key: [] for key in contexts}
        for link in self.context_links:
            if link.chunk_id not in chunks or link.context_id not in contexts:
                raise ValueError("context link must reference an existing child and context")
            if link.chunk_id in linked:
                raise ValueError("each child must have exactly one context link")
            linked.add(link.chunk_id)
            ordinals[link.context_id].append(link.child_ordinal)
            child, parent = chunks[link.chunk_id], contexts[link.context_id]
            for field in (
                "document_id", "document_version", "document_type", "title",
                "section_path", "model_id", "source_uri",
            ):
                if getattr(child, field) != getattr(parent, field):
                    raise ValueError("child and context provenance must agree")
            if (child.page_start is None) != (parent.page_start is None):
                raise ValueError("child and context page provenance must agree")
            if child.page_start is not None and not (
                parent.page_start <= child.page_start <= child.page_end <= parent.page_end
            ):
                raise ValueError("child page range must be inside its context")
        if linked != chunks.keys():
            raise ValueError("each child must have exactly one context link")
        if any(not values or values != list(range(len(values))) for values in ordinals.values()):
            raise ValueError("each context requires contiguous child ordinals from zero")
        return self


class IndexBuildStrategy(Protocol):
    """Prepare search chunks; storage and ingestion remain separate concerns."""

    name: str

    def build(self, document: KnowledgeDocument) -> PreparedKnowledge:
        ...
