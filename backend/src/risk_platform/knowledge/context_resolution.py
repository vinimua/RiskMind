"""PostgreSQL-only context resolution over a fixed child-retrieval snapshot."""

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.indexing.contracts import KnowledgeContextUnit
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeContextLinkRow, KnowledgeContextRow
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot


class ContextResolutionIntegrityError(ValueError):
    """Missing or inconsistent authoritative context; never return partial success."""


class ContextResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: RetrievalRouteSnapshot
    contexts: tuple[KnowledgeContext, ...]


async def _resolve(session: AsyncSession, result: DenseChildRetrievalResult) -> tuple[KnowledgeContext, ...]:
    hits = [item.hit for item in result.hits]
    ids = [hit.source.chunk_id for hit in hits]
    if len(set(ids)) != len(ids) or [hit.rank for hit in hits] != list(range(1, len(hits) + 1)):
        raise ContextResolutionIntegrityError("child hits must have unique IDs and contiguous ordered ranks")
    if not hits:
        return ()
    profile = result.snapshot.index_profile.value
    rows = list((await session.execute(select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == profile, KnowledgeChunkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())
    children = {row.chunk_id: row for row in rows}
    if set(children) != set(ids):
        raise ContextResolutionIntegrityError("authoritative child is missing from the snapshot profile")
    for hit in hits:
        child = children[hit.source.chunk_id]
        if (hit.source.document_id, hit.source.document_version) != (child.document_id, child.document_version):
            raise ContextResolutionIntegrityError("child source identity differs from PostgreSQL")
        if any(getattr(hit, key) != getattr(child, key) for key in (
            "document_type", "model_id", "title", "section_path", "content",
        )):
            raise ContextResolutionIntegrityError("child hit payload differs from PostgreSQL")
        KnowledgeChunk.model_validate({key: getattr(child, key) for key in KnowledgeChunk.model_fields})
    if result.snapshot.index_profile == IndexProfile.BASELINE:
        return tuple(KnowledgeContext(context_id=hit.source.chunk_id, content=children[hit.source.chunk_id].content,
                                      supporting_hits=(hit,)) for hit in hits)

    links = list((await session.execute(select(KnowledgeContextLinkRow).where(
        KnowledgeContextLinkRow.index_profile == profile, KnowledgeContextLinkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())
    by_child = {link.chunk_id: link for link in links}
    if len(links) != len(ids) or set(by_child) != set(ids):
        raise ContextResolutionIntegrityError("every retrieved child requires exactly one parent link")
    context_ids = {link.context_id for link in links}
    parents = list((await session.execute(select(KnowledgeContextRow).where(
        KnowledgeContextRow.index_profile == profile, KnowledgeContextRow.context_id.in_(context_ids),
    ).execution_options(populate_existing=True))).scalars())
    by_parent = {parent.context_id: parent for parent in parents}
    if set(by_parent) != context_ids:
        raise ContextResolutionIntegrityError("linked parent is missing from the snapshot profile")
    for parent in parents:
        KnowledgeContextUnit.model_validate({key: getattr(parent, key) for key in KnowledgeContextUnit.model_fields})
    grouped = {}
    ordinals = set()
    for hit in hits:
        child = children[hit.source.chunk_id]
        link = by_child[hit.source.chunk_id]
        parent = by_parent[link.context_id]
        ordinal_key = (parent.context_id, link.child_ordinal)
        if link.child_ordinal < 0 or ordinal_key in ordinals:
            raise ContextResolutionIntegrityError("parent links contain invalid child ordinals")
        ordinals.add(ordinal_key)
        if any(getattr(child, key) != getattr(parent, key) for key in (
            "document_id", "document_version", "document_type", "title", "section_path", "model_id", "source_uri",
        )):
            raise ContextResolutionIntegrityError("child and parent provenance differ")
        if (child.page_start is None) != (parent.page_start is None) or (
            child.page_start is not None and not (
                parent.page_start <= child.page_start <= child.page_end <= parent.page_end
            )
        ):
            raise ContextResolutionIntegrityError("child page span is outside its parent")
        grouped.setdefault(parent.context_id, []).append(hit)
    return tuple(KnowledgeContext(context_id=key, content=by_parent[key].content,
                                  supporting_hits=tuple(support)) for key, support in grouped.items())


async def resolve_knowledge_contexts(
    result: DenseChildRetrievalResult, *, session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ContextResolutionResult:
    """Preserve the request snapshot; never query routes, Milvus, embeddings or READY.

    Parent order follows first/best child rank. Supporting hits retain their
    original child ranks, with no score aggregation or parent-as-hit conversion.
    """
    result = DenseChildRetrievalResult.model_validate_json(result.model_dump_json())
    if not result.hits:
        return ContextResolutionResult(snapshot=result.snapshot, contexts=())
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        contexts = await _resolve(session, result)
    return ContextResolutionResult(snapshot=result.snapshot, contexts=contexts)
