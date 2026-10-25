"""Snapshot-bound dense child retrieval with authoritative PostgreSQL validation."""

import math
from numbers import Real

from pydantic import BaseModel, ConfigDict, Field
from pymilvus import MilvusClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.chunking import KnowledgeChunk
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSearchRequest, KnowledgeSourceRef
from risk_platform.knowledge.embeddings import EMBEDDING_DIMENSION, EmbeddingClient, get_embedding_client
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.serving import resolve_serving_route
from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot


class ChildRetrievalIntegrityError(ValueError):
    """The read model cannot be reconciled with authoritative child data."""


class RetrievedKnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hit: KnowledgeHit
    score: float = Field(allow_inf_nan=False)


class DenseChildRetrievalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: RetrievalRouteSnapshot
    hits: tuple[RetrievedKnowledgeHit, ...]


def _matches_filters(child: KnowledgeChunk, request: KnowledgeSearchRequest) -> bool:
    filters = request.filters
    return (
        (not filters.document_types or child.document_type in filters.document_types)
        and all(getattr(filters, key) is None or getattr(child, key) == getattr(filters, key)
                for key in ("document_id", "model_id", "document_version"))
    )


async def _authoritative_hits(
    session: AsyncSession, snapshot: RetrievalRouteSnapshot,
    request: KnowledgeSearchRequest, candidates: list[dict],
) -> tuple[RetrievedKnowledgeHit, ...]:
    if len(candidates) > request.top_k:
        raise ChildRetrievalIntegrityError("dense search returned more than top_k children")
    ids = [candidate.get("chunk_id") for candidate in candidates]
    if any(not isinstance(key, str) or not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ChildRetrievalIntegrityError("dense search returned invalid or duplicate child IDs")
    if not ids:
        return ()
    rows = list((await session.execute(select(KnowledgeChunkRow).where(
        KnowledgeChunkRow.index_profile == snapshot.index_profile.value,
        KnowledgeChunkRow.chunk_id.in_(ids),
    ).execution_options(populate_existing=True))).scalars())
    by_id = {row.chunk_id: row for row in rows}
    if set(by_id) != set(ids):
        raise ChildRetrievalIntegrityError("dense search child is missing from the authoritative profile")
    hits = []
    previous_score = math.inf
    for rank, candidate in enumerate(candidates, 1):
        score = candidate.get("score")
        if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score):
            raise ChildRetrievalIntegrityError("dense search returned a non-finite or invalid score")
        if score > previous_score:
            raise ChildRetrievalIntegrityError("cosine hits must be ordered by descending score")
        previous_score = score
        row = by_id[candidate["chunk_id"]]
        payload = {field: getattr(row, field) for field in milvus.OUTPUT_FIELDS}
        if any(field not in candidate or candidate[field] != value for field, value in payload.items()):
            raise ChildRetrievalIntegrityError("Milvus child payload differs from authoritative PostgreSQL data")
        child = KnowledgeChunk.model_validate(payload)
        if not _matches_filters(child, request):
            raise ChildRetrievalIntegrityError("authoritative child does not match requested filters")
        hits.append(RetrievedKnowledgeHit(
            hit=KnowledgeHit(
                source=KnowledgeSourceRef(document_id=child.document_id,
                                          document_version=child.document_version, chunk_id=child.chunk_id),
                document_type=child.document_type, model_id=child.model_id, title=child.title,
                section_path=child.section_path, content=child.content, rank=rank,
            ), score=float(score),
        ))
    return tuple(hits)


async def retrieve_dense_children(
    request: KnowledgeSearchRequest, *, index_profile: IndexProfile,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    embedding_client: EmbeddingClient | None = None, milvus_client: MilvusClient | None = None,
) -> DenseChildRetrievalResult:
    """Resolve once, search that physical collection, return only validated children.

    Missing routes and dependency failures propagate explicitly; integrity errors
    fail the whole request. No alias fallback, partial success or parent lookup.
    """
    request = KnowledgeSearchRequest.model_validate_json(request.model_dump_json())
    profile = IndexProfile(index_profile)
    sessions = session_factory or get_session_factory()
    async with sessions() as session:
        snapshot = await resolve_serving_route(session, profile)
    embedder = embedding_client or get_embedding_client()
    vector = await embedder.embed_query(request.query)
    if len(vector) != EMBEDDING_DIMENSION or any(
        isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) for value in vector
    ) or not any(vector):
        raise ChildRetrievalIntegrityError("query embedding must be a finite, nonzero 1024-dimensional vector")

    def search():
        client = milvus_client or milvus.get_client()
        if not client.has_collection(snapshot.collection):
            raise ChildRetrievalIntegrityError("serving physical collection is missing")
        milvus.validate_collection_for_profile(client, snapshot.collection, snapshot.index_profile)
        return milvus.dense_search(client, vector, request.top_k, filters=request.filters,
                                   collection=snapshot.collection)

    candidates = await milvus.acall(search)
    async with sessions() as session:
        hits = await _authoritative_hits(session, snapshot, request, candidates)
    return DenseChildRetrievalResult(snapshot=snapshot, hits=hits)
