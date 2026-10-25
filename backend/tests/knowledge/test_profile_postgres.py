"""Opt-in PostgreSQL migration/concurrency tests in an isolated temporary schema."""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from pymilvus import MilvusClient

from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeCollectionRow
from risk_platform.knowledge import milvus
from risk_platform.knowledge.ingestion import ingest_pending
from risk_platform.knowledge.repository import DocumentVersionConflictError, bind_collection, stage_prepared
from .test_profile_migration import migration_config
from .test_profile_persistence import document, ProfileMilvus
from .test_ingestion import FakeEmbeddingClient


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 for real PostgreSQL migration and advisory locks",
)


async def exercise(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    doc = document("Evidence. " * 100)
    prepared = ParentChildIndexBuildStrategy().build(doc)
    first_staged, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def first_writer():
        async with sessions() as session:
            async with session.begin():
                count = await stage_prepared(session, doc, prepared, index_profile=IndexProfile.PARENT_CHILD)
                first_staged.set()
                await release.wait()
                return count

    async def second_writer():
        await first_staged.wait()
        async with sessions() as session:
            async with session.begin():
                second_started.set()
                return await stage_prepared(session, doc, prepared, index_profile=IndexProfile.PARENT_CHILD)

    tasks = [asyncio.create_task(first_writer()), asyncio.create_task(second_writer())]
    try:
        await asyncio.wait_for(second_started.wait(), 10)
        done, _ = await asyncio.wait({tasks[1]}, timeout=0.2)
        assert not done, "second writer must wait for the source advisory lock"
        release.set()
        assert await asyncio.wait_for(tasks[0], 10) == len(prepared.search_chunks)
        assert await asyncio.wait_for(tasks[1], 10) == 0
        async with sessions() as session:
            async with session.begin():
                assert await stage_prepared(session, doc, BaselineIndexBuildStrategy().build(doc),
                                            index_profile=IndexProfile.BASELINE) > 0
        changed = document("Changed source.")
        async with sessions() as session:
            with pytest.raises(DocumentVersionConflictError):
                async with session.begin():
                    await stage_prepared(session, changed, BaselineIndexBuildStrategy().build(changed),
                                         index_profile=IndexProfile.BASELINE)

        async def compete(profile):
            async with sessions() as session:
                async with session.begin():
                    await bind_collection(session, collection="same_target", index_profile=profile, purpose="INGESTION")
        results = await asyncio.gather(compete(IndexProfile.BASELINE), compete(IndexProfile.PARENT_CHILD),
                                       return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


async def verify_migrated_ingestion(url):
    """Use real Milvus when explicitly enabled; never call an embedding API."""
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actual_milvus = os.environ.get("RUN_INGESTION_INTEGRATION") == "1"
    client = None
    names = [f"riskmind_6c_{kind}_{uuid4().hex}" for kind in ("baseline", "pc", "conflict")]
    try:
        client = await milvus.acall(
            lambda: MilvusClient(uri=os.environ["MILVUS_URI"]) if actual_milvus else ProfileMilvus()
        )
        await milvus.acall(lambda: milvus.ensure_collection(
            client, names[2], index_profile=IndexProfile.BASELINE,
        ))
        with pytest.raises(ValueError, match="another or unknown index profile"):
            await ingest_pending(
                collection=names[2], index_profile=IndexProfile.PARENT_CHILD,
                session_factory=sessions, embedding_client=FakeEmbeddingClient(), milvus_client=client,
            )
        async with sessions() as session:
            assert await session.get(KnowledgeCollectionRow, names[2]) is None
            assert list((await session.execute(select(KnowledgeCollectionRow))).scalars()) == []
        # Simulate the migration-period legacy Baseline collection without a marker.
        await milvus.acall(lambda: milvus.ensure_collection(client, names[0]))
        async with sessions() as session:
            before = list((await session.execute(select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.index_profile == "baseline-v1",
            ))).scalars())
        expected_pending = sum(row.ingestion_status == "PENDING" for row in before)
        assert expected_pending > 0
        count = await ingest_pending(
            collection=names[0], session_factory=sessions,
            embedding_client=FakeEmbeddingClient(), milvus_client=client,
        )
        assert count == expected_pending
        async with sessions() as session:
            rows = list((await session.execute(select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.index_profile == "baseline-v1",
            ))).scalars())
            assert all(row.ingestion_status == "READY" for row in rows)
            assert {row.chunk_id: row.content for row in rows} == {
                row.chunk_id: row.content for row in before
            }
        doc = document("Parent-Child after migration.").model_copy(update={"document_id": "REPORT-INGEST"})
        prepared = ParentChildIndexBuildStrategy().build(doc)
        async with sessions() as session:
            async with session.begin():
                assert await stage_prepared(session, doc, prepared, index_profile=IndexProfile.PARENT_CHILD) > 0
        assert await ingest_pending(
            collection=names[1], index_profile=IndexProfile.PARENT_CHILD, session_factory=sessions,
            embedding_client=FakeEmbeddingClient(), milvus_client=client,
        ) > 0
        async with sessions() as session:
            stored = list((await session.execute(select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.index_profile == "parent-child-v1",
            ))).scalars())
            assert stored and all(row.ingestion_status == "READY" for row in stored)
        if actual_milvus:
            def verify():
                description = client.describe_collection(names[1])
                assert description["description"] == "riskmind:index-profile:parent-child-v1"
                counts = client.query(names[1], filter="", output_fields=["count(*)"], consistency_level="Strong")
                assert counts[0]["count(*)"] == len(stored)
            await milvus.acall(verify)
    finally:
        if client is not None and actual_milvus:
            def cleanup():
                for name in names:
                    if client.has_collection(name):
                        client.drop_collection(name)
                client.close()
            await milvus.acall(cleanup)
        await engine.dispose()


def test_real_migration_preserves_legacy_rows_and_serializes_profile_writers(monkeypatch):
    # UUID-derived schema is the only DDL cleanup target; never migrate the user's
    # default/public schema as part of this test.
    schema = f"test_knowledge_{uuid4().hex}"
    url = make_url(os.environ["RISK_DATABASE_URL"])
    sync_engine = create_engine(url)
    isolated_url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        with sync_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("RISK_DATABASE_URL", isolated_url.render_as_string(hide_password=False))
        command.upgrade(migration_config(), "20261017_0001")
        isolated_engine = create_engine(isolated_url)
        try:
            with isolated_engine.begin() as connection:
                connection.execute(text("""INSERT INTO knowledge_chunks
                    (chunk_id,document_id,document_version,document_type,title,section_path,content,source_uri,ingestion_status)
                    VALUES ('legacy-ready','legacy','v1','MODEL_DEVELOPMENT_REPORT','Legacy','','Ready text.','knowledge://legacy','READY'),
                           ('legacy-pending','legacy','v1','MODEL_DEVELOPMENT_REPORT','Legacy','','Pending text.','knowledge://legacy','PENDING')"""))
            command.upgrade(migration_config(), "head")
            with isolated_engine.connect() as connection:
                row = connection.execute(select(KnowledgeChunkRow.chunk_id, KnowledgeChunkRow.content,
                                                KnowledgeChunkRow.index_profile, KnowledgeChunkRow.ingestion_status)
                                         .order_by(KnowledgeChunkRow.chunk_id)).all()
                assert [tuple(item) for item in row] == [
                    ("legacy-pending", "Pending text.", "baseline-v1", "PENDING"),
                    ("legacy-ready", "Ready text.", "baseline-v1", "READY"),
                ]
            if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(exercise(isolated_url))
            # Retire only this disposable schema's concurrency-test bindings so
            # the independent ingestion scenario has unbound profile targets.
            with isolated_engine.begin() as connection:
                connection.execute(text("DELETE FROM knowledge_collections"))
            asyncio.run(verify_migrated_ingestion(isolated_url))
        finally:
            isolated_engine.dispose()
    finally:
        with sync_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync_engine.dispose()
