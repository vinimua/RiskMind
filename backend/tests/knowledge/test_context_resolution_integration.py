import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from pymilvus import MilvusClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge import milvus
from risk_platform.knowledge.context_resolution import resolve_knowledge_contexts
from risk_platform.knowledge.contracts import KnowledgeSearchRequest
from risk_platform.knowledge.indexing import ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import rebuild_collection
from risk_platform.knowledge.retrieval import retrieve_dense_children
from risk_platform.knowledge.serving import resolve_serving_route, validate_serving_collection
from .test_context_resolution import parent_document
from .test_profile_migration import migration_config
from .test_profile_persistence import document, stage
from .test_retrieval_integration import FixedEmbedding
from .test_serving import activate


pytestmark = pytest.mark.skipif(
    any(os.environ.get(name) != "1" for name in ("RUN_POSTGRES_INTEGRATION", "RUN_MILVUS_INTEGRATION")),
    reason="enable PostgreSQL and Milvus integration switches for complete context pipelines",
)


async def exercise(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = await milvus.acall(lambda: MilvusClient(uri=os.environ["MILVUS_URI"]))
    names = []
    try:
        doc = parent_document()
        prepared = ParentChildIndexBuildStrategy().build(doc)
        links = {link.chunk_id: link.context_id for link in prepared.context_links}
        parents = {parent.context_id: parent.content for parent in prepared.context_units}
        for profile in IndexProfile:
            first, second = [f"riskmind_context_{uuid4().hex}" for _ in range(2)]
            names.extend((first, second))
            await stage(sessions, doc, profile)
            options = dict(index_profile=profile, session_factory=sessions, milvus_client=client,
                           embedding_client=FixedEmbedding())
            await rebuild_collection(collection=first, **options)
            receipt = await validate_serving_collection(collection=first, index_profile=profile,
                                                        session_factory=sessions, milvus_client=client)
            await activate(sessions, client, receipt, key=f"{profile}-v1")
            retrieved = await retrieve_dense_children(KnowledgeSearchRequest(query="find evidence", top_k=64), **options)
            resolved = await resolve_knowledge_contexts(retrieved, session_factory=sessions)
            assert resolved.snapshot == retrieved.snapshot
            assert len(retrieved.hits) > 3
            if profile == IndexProfile.BASELINE:
                assert [context.context_id for context in resolved.contexts] == [
                    item.hit.source.chunk_id for item in retrieved.hits
                ]
                assert [context.supporting_hits for context in resolved.contexts] == [(item.hit,) for item in retrieved.hits]
            else:
                order = list(dict.fromkeys(links[item.hit.source.chunk_id] for item in retrieved.hits))
                assert [context.context_id for context in resolved.contexts] == order
                assert len(resolved.contexts) == 3
                for context in resolved.contexts:
                    assert context.content == parents[context.context_id]
                    assert context.supporting_hits == tuple(item.hit for item in retrieved.hits
                                                           if links[item.hit.source.chunk_id] == context.context_id)
            await stage(sessions, document("New source", version="v2"), profile)
            await rebuild_collection(collection=second, **options)
            newer = await validate_serving_collection(collection=second, index_profile=profile,
                                                      session_factory=sessions, milvus_client=client)
            await activate(sessions, client, newer, key=f"{profile}-v2", expected=1)
            async with sessions() as session:
                assert (await resolve_serving_route(session, profile)).route_version == 2
            assert await resolve_knowledge_contexts(retrieved, session_factory=sessions) == resolved
    finally:
        def cleanup():
            for name in names:
                if client.has_collection(name):
                    client.drop_collection(name)
            client.close()
        await milvus.acall(cleanup)
        await engine.dispose()


def test_real_child_to_context_pipelines_preserve_old_snapshot(monkeypatch):
    schema = f"test_context_{uuid4().hex}"
    root = create_engine(os.environ["RISK_DATABASE_URL"])
    url = make_url(os.environ["RISK_DATABASE_URL"]).update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        with root.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("RISK_DATABASE_URL", url.render_as_string(hide_password=False))
        command.upgrade(migration_config(), "head")
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(exercise(url))
    finally:
        with root.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        root.dispose()
