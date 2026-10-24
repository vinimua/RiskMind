"""Relational integration tests; SQLite exercises SQL/FKs, not PG advisory locks."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from risk_platform.db.database import Base
from risk_platform.knowledge.chunking import DocumentPage, KnowledgeDocument, chunk_document
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import ingest_pending, rebuild_collection
from risk_platform.knowledge.models import (
    KnowledgeBuildRow, KnowledgeChunkRow, KnowledgeCollectionRow,
    KnowledgeContextLinkRow, KnowledgeContextRow, KnowledgeDocumentRow,
)
from risk_platform.knowledge.repository import (
    DocumentVersionConflictError, bind_collection, count_chunks, list_pending_chunks,
    mark_chunks_ready, stage_chunks, stage_prepared,
)
from .test_ingestion import FakeEmbeddingClient
from .test_milvus import FakeMilvusClient


class AsyncFacade:
    """Run real synchronous SQL via the repository's small async session surface."""

    def __init__(self, engine):
        self.session = Session(engine, expire_on_commit=False)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.session.close()

    @asynccontextmanager
    async def begin(self):
        with self.session.begin():
            yield

    async def execute(self, statement, parameters=None):
        if str(statement).startswith("SELECT pg_advisory_xact_lock"):
            return None
        return self.session.execute(statement, parameters or {})

    async def get(self, model, key):
        return self.session.get(model, key)

    def add(self, row):
        self.session.add(row)

    def add_all(self, rows):
        self.session.add_all(rows)

    async def flush(self):
        self.session.flush()


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield lambda: AsyncFacade(engine)
    engine.dispose()


def document(content="Text.", version="v1"):
    return KnowledgeDocument(
        document_id="REPORT-1", document_version=version,
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        title="Report", source_uri="knowledge://reports/1", model_id="credit_v3",
        pages=(DocumentPage(content="# Report\n## Analysis\n" + content),),
    )


async def stage(sessions, doc, profile):
    prepared = (BaselineIndexBuildStrategy() if profile == IndexProfile.BASELINE
                else ParentChildIndexBuildStrategy()).build(doc)
    async with sessions() as session:
        async with session.begin():
            return await stage_prepared(session, doc, prepared, index_profile=profile)


def test_both_profiles_persist_atomically_and_retries_preserve_ready(sessions):
    async def exercise():
        doc = document("Evidence. " * 150)
        assert await stage(sessions, doc, IndexProfile.BASELINE) > 0
        assert await stage(sessions, doc, IndexProfile.PARENT_CHILD) > 0
        async with sessions() as session:
            async with session.begin():
                baseline = await list_pending_chunks(session, limit=100)
                pc = await list_pending_chunks(session, limit=100, index_profile=IndexProfile.PARENT_CHILD)
                assert baseline and pc
                assert {r.chunk_id for r in baseline}.isdisjoint(r.chunk_id for r in pc)
                assert await mark_chunks_ready(session, [pc[0].chunk_id]) == 0
                assert await mark_chunks_ready(session, [pc[0].chunk_id], index_profile=IndexProfile.PARENT_CHILD) == 1
        assert await stage(sessions, doc, IndexProfile.PARENT_CHILD) == 0
        async with sessions() as session:
            assert (await session.get(KnowledgeChunkRow, (pc[0].chunk_id, "parent-child-v1"))).ingestion_status == "READY"
            assert len(list((await session.execute(select(KnowledgeDocumentRow))).scalars())) == 1
            assert len(list((await session.execute(select(KnowledgeBuildRow))).scalars())) == 2
            parents = list((await session.execute(select(KnowledgeContextRow))).scalars())
            links = list((await session.execute(select(KnowledgeContextLinkRow))).scalars())
            assert len(parents) == 1
            assert len(links) == len(pc)
            assert [l.child_ordinal for l in links] == list(range(len(pc)))
            assert parents[0].content == "Evidence. " * 149 + "Evidence."
    asyncio.run(exercise())


def test_cross_profile_source_immutability_and_legacy_enrollment(sessions):
    async def exercise():
        original = document()
        async with sessions() as session:
            async with session.begin():
                await stage_chunks(session, chunk_document(original))
        with pytest.raises(DocumentVersionConflictError):
            await stage(sessions, document("Changed."), IndexProfile.PARENT_CHILD)
        await stage(sessions, original, IndexProfile.PARENT_CHILD)
        with pytest.raises(DocumentVersionConflictError):
            await stage(sessions, document("Changed."), IndexProfile.BASELINE)
        async with sessions() as session:
            async with session.begin():
                with pytest.raises(DocumentVersionConflictError):
                    await stage_chunks(session, chunk_document(document("Changed.")))
        await stage(sessions, document("Changed.", version="v2"), IndexProfile.BASELINE)
    asyncio.run(exercise())


def test_parent_child_first_prevents_legacy_baseline_source_replacement(sessions):
    async def exercise():
        await stage(sessions, document(), IndexProfile.PARENT_CHILD)
        async with sessions() as session:
            async with session.begin():
                with pytest.raises(DocumentVersionConflictError):
                    await stage_chunks(session, chunk_document(document("Changed.")))
                assert await stage_chunks(session, chunk_document(document())) == 1
    asyncio.run(exercise())


def test_staging_rolls_back_source_children_contexts_links_and_build(sessions):
    async def exercise():
        async with sessions() as session:
            with pytest.raises(RuntimeError, match="cancel"):
                async with session.begin():
                    doc = document()
                    await stage_prepared(session, doc, ParentChildIndexBuildStrategy().build(doc),
                                         index_profile=IndexProfile.PARENT_CHILD)
                    raise RuntimeError("cancel")
        async with sessions() as session:
            for model in (KnowledgeDocumentRow, KnowledgeBuildRow, KnowledgeChunkRow,
                          KnowledgeContextRow, KnowledgeContextLinkRow):
                assert (await session.execute(select(func.count()).select_from(model))).scalar_one() == 0
    asyncio.run(exercise())


def test_wrong_profile_artifact_and_unknown_profile_fail_before_writes(sessions):
    async def exercise():
        doc = document()
        async with sessions() as session:
            async with session.begin():
                for profile in (IndexProfile.PARENT_CHILD, "unknown"):
                    with pytest.raises(ValueError):
                        await stage_prepared(session, doc, BaselineIndexBuildStrategy().build(doc),
                                             index_profile=profile)
                with pytest.raises(ValueError, match="must use stage_prepared"):
                    await stage_chunks(session, ParentChildIndexBuildStrategy().build(doc).search_chunks)
        async with sessions() as session:
            assert await count_chunks(session) == 0
    asyncio.run(exercise())


def test_collection_ownership_purpose_and_single_ingestion_target(sessions):
    async def bind(name, profile, purpose):
        async with sessions() as session:
            async with session.begin():
                await bind_collection(session, collection=name, index_profile=profile, purpose=purpose)

    async def exercise():
        await bind("baseline", IndexProfile.BASELINE, "INGESTION")
        await bind("baseline", IndexProfile.BASELINE, "INGESTION")
        await bind("pc", IndexProfile.PARENT_CHILD, "INGESTION")
        for profile, purpose in ((IndexProfile.PARENT_CHILD, "INGESTION"), (IndexProfile.BASELINE, "REBUILD")):
            with pytest.raises(ValueError):
                await bind("baseline", profile, purpose)
        with pytest.raises(ValueError, match="already has an ingestion target"):
            await bind("baseline2", IndexProfile.BASELINE, "INGESTION")
        await bind("rebuild1", IndexProfile.BASELINE, "REBUILD")
        await bind("rebuild2", IndexProfile.BASELINE, "REBUILD")
    asyncio.run(exercise())


def test_database_rejects_dangling_links_and_negative_ordinals(sessions):
    async def exercise():
        await stage(sessions, document(), IndexProfile.PARENT_CHILD)
        async with sessions() as session:
            parent = (await session.execute(select(KnowledgeContextRow))).scalar_one()
        for ordinal in (0, -1):
            async with sessions() as session:
                with pytest.raises(IntegrityError):
                    async with session.begin():
                        session.add(KnowledgeContextLinkRow(
                            index_profile="parent-child-v1", chunk_id="missing",
                            context_id=parent.context_id, child_ordinal=ordinal,
                        ))
                        await session.flush()
    asyncio.run(exercise())


class ProfileMilvus(FakeMilvusClient):
    def __init__(self):
        super().__init__()
        self.collections = {}
        self.fail_flush = False

    def has_collection(self, collection):
        return collection in self.collections

    def create_collection(self, collection, **kwargs):
        self.collections[collection] = self.schema_options.get("description", "")

    def describe_collection(self, collection):
        return {"collection_name": collection, "description": self.collections[collection]}

    def flush(self, collection):
        if self.fail_flush:
            raise RuntimeError("flush failed")
        super().flush(collection)


def test_profile_ingestion_retry_and_rebuild_use_only_children(sessions, monkeypatch):
    from risk_platform.knowledge import milvus

    async def inline(fn):
        return fn()
    monkeypatch.setattr(milvus, "acall", inline)

    async def exercise():
        doc = document("Evidence. " * 150)
        await stage(sessions, doc, IndexProfile.BASELINE)
        await stage(sessions, doc, IndexProfile.PARENT_CHILD)
        client = ProfileMilvus()
        embedder = FakeEmbeddingClient()
        options = dict(collection="pc_ingestion", index_profile=IndexProfile.PARENT_CHILD,
                       session_factory=sessions, embedding_client=embedder, milvus_client=client)
        client.fail_flush = True
        with pytest.raises(RuntimeError, match="flush failed"):
            await ingest_pending(**options)
        async with sessions() as session:
            pc = await list_pending_chunks(session, limit=100, index_profile=IndexProfile.PARENT_CHILD)
            assert pc
        client.fail_flush = False
        assert await ingest_pending(**options) == len(pc)
        assert await ingest_pending(**options) == 0
        assert client.collections["pc_ingestion"] == "riskmind:index-profile:parent-child-v1"
        assert all(row["chunk_id"].startswith("pc-child-") for _, rows in client.upserts for row in rows)
        before = len(client.upserts)
        assert await rebuild_collection(**{**options, "collection": "pc_rebuild"}) == len(pc)
        assert len(client.upserts) > before
        async with sessions() as session:
            assert not await list_pending_chunks(session, limit=100, index_profile=IndexProfile.PARENT_CHILD)
            assert await list_pending_chunks(session, limit=100)  # Baseline is untouched.
        with pytest.raises(ValueError, match="another profile or purpose"):
            await ingest_pending(**{**options, "index_profile": IndexProfile.BASELINE})
        with pytest.raises(ValueError, match="already has an ingestion target"):
            await ingest_pending(**{**options, "collection": "pc_other"})
    asyncio.run(exercise())


def test_post_flush_database_failure_retries_identical_profile_chunk_ids(sessions, monkeypatch):
    from risk_platform.knowledge import ingestion, milvus

    async def inline(fn):
        return fn()
    monkeypatch.setattr(milvus, "acall", inline)

    async def exercise():
        await stage(sessions, document(), IndexProfile.PARENT_CHILD)
        client = ProfileMilvus()
        options = dict(collection="pc", index_profile=IndexProfile.PARENT_CHILD,
                       session_factory=sessions, embedding_client=FakeEmbeddingClient(), milvus_client=client)
        original = ingestion.mark_chunks_ready

        async def fail(*args, **kwargs):
            raise RuntimeError("database unavailable after flush")
        monkeypatch.setattr(ingestion, "mark_chunks_ready", fail)
        with pytest.raises(RuntimeError, match="after flush"):
            await ingest_pending(**options)
        assert client.flushed == ["pc"]
        async with sessions() as session:
            assert await list_pending_chunks(session, limit=10, index_profile=IndexProfile.PARENT_CHILD)
        monkeypatch.setattr(ingestion, "mark_chunks_ready", original)
        assert await ingest_pending(**options) == 1
        assert [row["chunk_id"] for row in client.upserts[0][1]] == [
            row["chunk_id"] for row in client.upserts[1][1]
        ]
    asyncio.run(exercise())


def test_rebuild_retries_from_authoritative_children_without_rerunning_strategy(sessions, monkeypatch):
    from risk_platform.knowledge import milvus

    async def inline(fn):
        return fn()
    monkeypatch.setattr(milvus, "acall", inline)

    async def exercise():
        await stage(sessions, document("Evidence. " * 150), IndexProfile.PARENT_CHILD)

        def forbidden(*args):
            raise AssertionError("rebuild must read persisted chunks, never rebuild a strategy")
        monkeypatch.setattr(ParentChildIndexBuildStrategy, "build", forbidden)
        client = ProfileMilvus()
        original = client.upsert
        attempts = 0

        def flaky(collection, rows):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise RuntimeError("second batch failed")
            original(collection, rows)
        monkeypatch.setattr(client, "upsert", flaky)
        options = dict(collection="rebuilt", index_profile=IndexProfile.PARENT_CHILD, batch_size=1,
                       session_factory=sessions, embedding_client=FakeEmbeddingClient(), milvus_client=client)
        with pytest.raises(RuntimeError, match="second batch"):
            await rebuild_collection(**options)
        monkeypatch.setattr(client, "upsert", original)
        async with sessions() as session:
            count = await count_chunks(session, index_profile=IndexProfile.PARENT_CHILD)
        assert await rebuild_collection(**options) == count
        async with sessions() as session:
            assert len(await list_pending_chunks(session, limit=100, index_profile=IndexProfile.PARENT_CHILD)) == count
        assert client.upserts[0][1] == client.upserts[1][1]
    asyncio.run(exercise())
