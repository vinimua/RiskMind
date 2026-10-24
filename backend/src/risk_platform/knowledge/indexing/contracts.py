"""Minimal contracts for preparing searchable knowledge without persistence."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from risk_platform.knowledge.chunking import KnowledgeChunk, KnowledgeDocument


class PreparedKnowledge(BaseModel):
    """Ordered search chunks produced from one knowledge document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    search_chunks: tuple[KnowledgeChunk, ...]


class IndexBuildStrategy(Protocol):
    """Prepare search chunks; storage and ingestion remain separate concerns."""

    name: str

    def build(self, document: KnowledgeDocument) -> PreparedKnowledge:
        ...
