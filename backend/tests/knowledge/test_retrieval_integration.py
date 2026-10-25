import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from pymilvus import MilvusClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge import milvus
from risk_platform.knowledge.contracts import KnowledgeFilters, KnowledgeSearchRequest
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import rebuild_collection
from risk_platform.knowledge.retrieval import retrieve_dense_children
from risk_platform.knowledge.serving import validate_serving_collection
from .test_profile_migration import migration_config
from .test_profile_persistence import document, stage
from .test_retrieval import QueryEmbedding
from .test_serving import activate


pytestmark = pytest.mark.skipif(
    any(os.environ.get(name) != "1" for name in ("RUN_POSTGRES_INTEGRATION", "RUN_MILVUS_INTEGRATION")),
    reason="enable PostgreSQL and Milvus integration switches for real dense child retrieval",
)


class FixedEmbedding(QueryEmbedding):
    async def embed_texts(self, texts):
        return [self.vector for _ in texts]


async def exercise(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = await milvus.acall(lambda: MilvusClient(uri=os.environ["MILVUS_URI"]))
    collections = []
    try:
        for profile in IndexProfile:
            name = f"riskmind_child_retrieval_{uuid4().hex}"
            collections.append(name)
            await stage(sessions, document("Authoritative child text."), profile)
            await rebuild_collection(collection=name, index_profile=profile, session_factory=sessions,
                                     embedding_client=FixedEmbedding(), milvus_client=client)
            acceptance = await validate_serving_collection(collection=name, index_profile=profile,
                                                           session_factory=sessions, milvus_client=client)
            await activate(sessions, client, acceptance, key=f"activate-{profile.value}")
            # New authoritative data must not enter an already serving snapshot.
            await stage(sessions, document("Newly staged source", version="v2"), profile)
            result = await retrieve_dense_children(KnowledgeSearchRequest(
                query="Find child text", top_k=3, filters=KnowledgeFilters(document_version="v1")),
                index_profile=profile, session_factory=sessions, embedding_client=FixedEmbedding(), milvus_client=client)
            assert result.snapshot.collection == name and result.snapshot.index_profile == profile
            assert result.snapshot.route_version == 1
            assert len(result.hits) == 1
            assert result.hits[0].hit.content == "Authoritative child text."
            assert result.hits[0].hit.source.document_version == "v1"
            assert result.hits[0].score == pytest.approx(1.0, abs=1e-5)
            empty = await retrieve_dense_children(KnowledgeSearchRequest(
                query="query", top_k=3, filters=KnowledgeFilters(document_id="missing-document")),
                index_profile=profile, session_factory=sessions, embedding_client=FixedEmbedding(), milvus_client=client)
            assert empty.hits == ()
    finally:
        def cleanup():
            for collection in collections:
                if client.has_collection(collection):
                    client.drop_collection(collection)
            client.close()
        await milvus.acall(cleanup)
        await engine.dispose()


def test_real_dense_retrieval_uses_each_profile_route_and_pg_source(monkeypatch):
    schema = f"test_retrieval_{uuid4().hex}"
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
