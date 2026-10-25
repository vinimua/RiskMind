import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from risk_platform.knowledge import retrieval
from risk_platform.knowledge.contracts import KnowledgeFilters, KnowledgeSearchRequest
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.retrieval import ChildRetrievalIntegrityError, RetrievedKnowledgeHit, retrieve_dense_children
from risk_platform.knowledge.serving import MissingServingRouteError, resolve_serving_route
from .test_profile_persistence import ProfileMilvus, document, sessions, stage
from .test_serving import candidate, activate, inline_milvus_and_sqlite_timezones


class QueryEmbedding:
    def __init__(self, callback=None, vector=None):
        self.inputs = []
        self.callback = callback
        self.vector = vector if vector is not None else [1.0] + [0.0] * 1023

    async def embed_query(self, query):
        self.inputs.append(query)
        if self.callback:
            await self.callback()
        return self.vector


def remote_rows(client, collection):
    return [{field: row[field] for field in retrieval.milvus.OUTPUT_FIELDS}
            for row in client._rows(collection).values()]


def configure_search(client, rows):
    client.search_results = [[{"entity": row, "distance": 0.9 - index * 0.1}
                              for index, row in enumerate(rows)]]


@pytest.mark.parametrize("profile", list(IndexProfile))
def test_profile_route_and_pg_hit_provenance_without_ready_or_parent_resolution(sessions, profile):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client, "physical", profile)
        await activate(sessions, client, receipt)
        rows = remote_rows(client, "physical")
        configure_search(client, rows)
        embedder = QueryEmbedding()
        result = await retrieve_dense_children(KnowledgeSearchRequest(query="Find evidence", top_k=3),
                                               index_profile=profile, session_factory=sessions,
                                               milvus_client=client, embedding_client=embedder)
        assert result.snapshot.index_profile == profile
        assert result.snapshot.collection == "physical" and result.snapshot.route_version == 1
        assert embedder.inputs == ["Find evidence"]
        assert result.hits[0].score == 0.9
        assert result.hits[0].hit.source.chunk_id == rows[0]["chunk_id"]
        assert result.hits[0].hit.content == rows[0]["content"]
        assert result.hits[0].hit.rank == 1
        assert not result.hits[0].hit.source.chunk_id.startswith("pc-parent-")
        async with sessions() as session:
            assert (await session.execute(select(KnowledgeChunkRow))).scalar_one().ingestion_status == "PENDING"
        assert client.search_calls[0]["collection"] == "physical"
        assert client.search_calls[0]["anns_field"] == "dense_vector"
        with pytest.raises(ValidationError):
            result.snapshot = result.snapshot
    asyncio.run(exercise())


def test_missing_route_fails_before_embedding_and_never_uses_alias(sessions):
    async def exercise():
        embedder = QueryEmbedding()
        client = ProfileMilvus()
        with pytest.raises(MissingServingRouteError):
            await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1),
                                          index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                          embedding_client=embedder, milvus_client=client)
        assert embedder.inputs == [] and client.search_calls == []
    asyncio.run(exercise())


def test_route_resolved_once_and_stays_fixed_during_activation(sessions, monkeypatch):
    async def exercise():
        client = ProfileMilvus()
        first = await candidate(sessions, client, "v1")
        await activate(sessions, client, first)
        second = await candidate(sessions, client, "v2")
        configure_search(client, remote_rows(client, "v1"))
        resolver = AsyncMock(wraps=resolve_serving_route)
        monkeypatch.setattr(retrieval, "resolve_serving_route", resolver)

        async def switch():
            await activate(sessions, client, second, key="switch", expected=1)
        result = await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1),
                                              index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                              embedding_client=QueryEmbedding(switch), milvus_client=client)
        assert resolver.await_count == 1
        assert result.snapshot.collection == "v1" and result.snapshot.route_version == 1
        assert client.search_calls[0]["collection"] == "v1"
        async with sessions() as session:
            assert (await resolve_serving_route(session, IndexProfile.BASELINE)).route_version == 2
    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["missing-pg", "wrong-profile", "content", "document_version",
                                    "source_uri", "page_start", "duplicate", "nan", "ascending", "too-many"])
def test_inconsistent_remote_children_fail_entire_request(sessions, fault):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client, "physical")
        await activate(sessions, client, receipt)
        rows = remote_rows(client, "physical")
        if fault == "wrong-profile":
            await stage(sessions, document(), IndexProfile.PARENT_CHILD)
            async with sessions() as session:
                row = (await session.execute(select(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.index_profile == IndexProfile.PARENT_CHILD.value,
                ))).scalar_one()
                rows = [{field: getattr(row, field) for field in retrieval.milvus.OUTPUT_FIELDS}]
        elif fault == "missing-pg":
            rows[0]["chunk_id"] = "unknown-child"
        elif fault in ("content", "document_version", "source_uri", "page_start"):
            rows[0][fault] = "changed" if fault != "page_start" else 10
        elif fault in ("duplicate", "ascending", "too-many"):
            rows += [dict(rows[0])]
        configure_search(client, rows)
        if fault == "nan":
            client.search_results[0][0]["distance"] = float("nan")
        if fault == "ascending":
            # Distinct valid ID avoids the duplicate guard and reaches score validation.
            await stage(sessions, document("Other", version="v2"), IndexProfile.BASELINE)
            async with sessions() as session:
                row = (await session.execute(select(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.document_version == "v2",
                ))).scalar_one()
                client.search_results[0][1] = {"entity": {field: getattr(row, field) for field in retrieval.milvus.OUTPUT_FIELDS},
                                               "distance": 0.95}
        with pytest.raises(ChildRetrievalIntegrityError):
            await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1 if fault == "too-many" else 5),
                                          index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                          milvus_client=client, embedding_client=QueryEmbedding())
    asyncio.run(exercise())


def test_pg_filters_rechecked_and_empty_results_are_success(sessions):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client, "physical")
        await activate(sessions, client, receipt)
        configure_search(client, remote_rows(client, "physical"))
        request = KnowledgeSearchRequest(query="query", top_k=2, filters=KnowledgeFilters(document_id="OTHER"))
        with pytest.raises(ChildRetrievalIntegrityError, match="filters"):
            await retrieve_dense_children(request, index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                          milvus_client=client, embedding_client=QueryEmbedding())
        assert 'document_id == "OTHER"' in client.search_calls[0]["filter"]
        client.search_results = [[]]
        result = await retrieve_dense_children(request, index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                               milvus_client=client, embedding_client=QueryEmbedding())
        assert result.hits == () and result.snapshot.collection == "physical"
    asyncio.run(exercise())


@pytest.mark.parametrize("vector", [[0.0] * 1024, [1.0] * 2, [float("nan")] * 1024, [True] * 1024])
def test_invalid_embeddings_fail_before_search(sessions, vector):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client)
        await activate(sessions, client, receipt)
        with pytest.raises(ChildRetrievalIntegrityError, match="embedding"):
            await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1),
                                          index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                          milvus_client=client, embedding_client=QueryEmbedding(vector=vector))
        assert client.search_calls == []
    asyncio.run(exercise())


def test_old_snapshot_remains_readable_after_new_source_staged(sessions):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client)
        await activate(sessions, client, receipt)
        rows = remote_rows(client, "candidate")
        configure_search(client, rows)
        await stage(sessions, document("New source", version="v2"), IndexProfile.BASELINE)
        result = await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1),
                                              index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                              milvus_client=client, embedding_client=QueryEmbedding())
        assert result.hits[0].hit.source.document_version == "v1"
    asyncio.run(exercise())


@pytest.mark.parametrize("dependency", ["embedding", "milvus", "postgres", "missing-collection", "owner"])
def test_dependency_failures_never_become_empty_success(sessions, monkeypatch, dependency):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client)
        await activate(sessions, client, receipt)
        configure_search(client, remote_rows(client, "candidate"))
        embedder = QueryEmbedding()
        if dependency == "embedding":
            embedder.embed_query = AsyncMock(side_effect=RuntimeError("embedding unavailable"))
        elif dependency == "milvus":
            monkeypatch.setattr(client, "search", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("search unavailable")))
        elif dependency == "postgres":
            monkeypatch.setattr(retrieval, "_authoritative_hits", AsyncMock(side_effect=RuntimeError("database unavailable")))
        elif dependency == "missing-collection":
            client.collections.clear()
        else:
            client.collections["candidate"] = "riskmind:index-profile:parent-child-v1"
        with pytest.raises((RuntimeError, ChildRetrievalIntegrityError, ValueError)):
            await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=1),
                                          index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                          embedding_client=embedder, milvus_client=client)
    asyncio.run(exercise())


def test_rank_order_and_score_ties_follow_search_order(sessions):
    async def exercise():
        client = ProfileMilvus()
        await stage(sessions, document("Other source", version="v2"), IndexProfile.BASELINE)
        receipt = await candidate(sessions, client)
        await activate(sessions, client, receipt)
        rows = list(reversed(remote_rows(client, "candidate")))
        configure_search(client, rows)
        for hit in client.search_results[0]:
            hit["distance"] = 0.5
        result = await retrieve_dense_children(KnowledgeSearchRequest(query="query", top_k=2),
                                              index_profile=IndexProfile.BASELINE, session_factory=sessions,
                                              embedding_client=QueryEmbedding(), milvus_client=client)
        assert [item.hit.source.chunk_id for item in result.hits] == [row["chunk_id"] for row in rows]
        assert [item.hit.rank for item in result.hits] == [1, 2]
    asyncio.run(exercise())
