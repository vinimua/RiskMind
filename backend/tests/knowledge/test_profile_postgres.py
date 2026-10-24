"""Opt-in PostgreSQL migration/concurrency tests in an isolated temporary schema."""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow
from risk_platform.knowledge.repository import DocumentVersionConflictError, bind_collection, stage_prepared
from .test_profile_migration import migration_config
from .test_profile_persistence import document


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
                    VALUES ('legacy','legacy','v1','MODEL_DEVELOPMENT_REPORT','Legacy','','Text.','knowledge://legacy','READY')"""))
            command.upgrade(migration_config(), "head")
            with isolated_engine.connect() as connection:
                row = connection.execute(select(KnowledgeChunkRow.chunk_id, KnowledgeChunkRow.content,
                                                KnowledgeChunkRow.index_profile, KnowledgeChunkRow.ingestion_status)).one()
                assert tuple(row) == ("legacy", "Text.", "baseline-v1", "READY")
            if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(exercise(isolated_url))
        finally:
            isolated_engine.dispose()
    finally:
        with sync_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync_engine.dispose()
