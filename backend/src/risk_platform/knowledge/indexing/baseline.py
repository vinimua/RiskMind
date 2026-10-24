"""Index preparation using the frozen baseline chunking scheme."""

from risk_platform.knowledge.chunking import KnowledgeDocument, chunk_document
from risk_platform.knowledge.indexing.contracts import PreparedKnowledge


class BaselineIndexBuildStrategy:
    """Delegate to baseline chunking without changing chunks or their order."""

    name = "baseline-v1"

    def build(self, document: KnowledgeDocument) -> PreparedKnowledge:
        return PreparedKnowledge(search_chunks=tuple(chunk_document(document)))
