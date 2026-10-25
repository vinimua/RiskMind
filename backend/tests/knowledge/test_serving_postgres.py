"""Real migration, activation CAS, audit and writer serialization."""

import asyncio
import os
from uuid import uuid4

import pytest
from alembic import command
from pymilvus import MilvusClient
from sqlalchemy import create_engine, make_url, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError

from risk_platform.knowledge import milvus
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import rebuild_collection
from risk_platform.knowledge.models import KnowledgeServingActivationRow, KnowledgeServingRouteRow
from risk_platform.knowledge.serving import (
    MissingServingRouteError, ServingEligibilityError, activate_serving_collection,
    resolve_serving_route, validate_serving_collection,
)
from .test_ingestion import FakeEmbeddingClient
from .test_profile_migration import migration_config
from .test_profile_persistence import ProfileMilvus, document, stage


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
    reason="set RUN_POSTGRES_INTEGRATION=1 for real serving activation and concurrency",
)


async def exercise(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actual = os.environ.get("RUN_INGESTION_INTEGRATION") == "1"
    client = await milvus.acall(lambda: MilvusClient(uri=os.environ["MILVUS_URI"]) if actual else ProfileMilvus())
    names = [f"riskmind_serving_{uuid4().hex}" for _ in range(3)]
    try:
        await stage(sessions, document(), IndexProfile.BASELINE)
        receipts = []
        for name in names:
            await rebuild_collection(collection=name, session_factory=sessions,
                                     embedding_client=FakeEmbeddingClient(), milvus_client=client)
            receipts.append(await validate_serving_collection(
                collection=name, index_profile=IndexProfile.BASELINE, session_factory=sessions, milvus_client=client,
            ))
        async with sessions() as session:
            with pytest.raises(MissingServingRouteError):
                await resolve_serving_route(session, IndexProfile.BASELINE)

        async def activate(receipt, key, expected, allow=True):
            async with sessions() as session:
                async with session.begin():
                    return await activate_serving_collection(
                        session, build_id=receipt.build_id, actor_id="test-operator", idempotency_key=key,
                        expected_route_version=expected, authorize=lambda *_: allow, milvus_client=client,
                    )

        with pytest.raises(PermissionError):
            await activate(receipts[0], "denied", 0, False)
        first = await activate(receipts[0], "first", 0)
        results = await asyncio.gather(activate(receipts[1], "second-a", 1),
                                       activate(receipts[2], "second-b", 1), return_exceptions=True)
        assert sum(isinstance(result, ServingEligibilityError) for result in results) == 1
        assert sum(getattr(result, "route_version", None) == 2 for result in results) == 1
        assert await activate(receipts[0], "first", 0) == first
        async with sessions() as session:
            assert (await resolve_serving_route(session, IndexProfile.BASELINE)).route_version == 2
            assert len(list((await session.execute(select(KnowledgeServingActivationRow))).scalars())) == 2

        # An activation transaction holds the profile-source lock through commit.
        activated, writer_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def activation_transaction():
            async with sessions() as session:
                async with session.begin():
                    await activate_serving_collection(
                        session, build_id=receipts[0].build_id, actor_id="test-operator", idempotency_key="third",
                        expected_route_version=2, authorize=lambda *_: True, milvus_client=client,
                    )
                    activated.set()
                    await release.wait()

        async def writer():
            await activated.wait()
            writer_started.set()
            await stage(sessions, document("New source", version="v2"), IndexProfile.BASELINE)

        tasks = [asyncio.create_task(activation_transaction()), asyncio.create_task(writer())]
        try:
            await asyncio.wait_for(writer_started.wait(), 30)
            done, _ = await asyncio.wait({tasks[1]}, timeout=0.2)
            assert not done, "source writer must wait until activation commits"
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 30)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
        with pytest.raises(ServingEligibilityError, match="source snapshot changed"):
            await activate(receipts[1], "stale", 3)
        async with sessions() as session:
            assert (await resolve_serving_route(session, IndexProfile.BASELINE)).route_version == 3
    finally:
        if actual:
            def cleanup():
                for name in names:
                    if client.has_collection(name):
                        client.drop_collection(name)
                client.close()
            await milvus.acall(cleanup)
        await engine.dispose()


def test_upgrade_0003_and_real_activation_concurrency(monkeypatch):
    schema = f"test_serving_{uuid4().hex}"
    root_url = make_url(os.environ["RISK_DATABASE_URL"])
    root = create_engine(root_url)
    url = root_url.update_query_dict({"options": f"-csearch_path={schema}"})
    isolated = create_engine(url)
    try:
        with root.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("RISK_DATABASE_URL", url.render_as_string(hide_password=False))
        command.upgrade(migration_config(), "20261024_0002")
        with isolated.begin() as connection:
            connection.execute(text("INSERT INTO knowledge_collections VALUES ('legacy-binding','baseline-v1','REBUILD')"))
        command.upgrade(migration_config(), "head")
        with isolated.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM knowledge_collections")).scalar_one() == 1
            assert connection.execute(select(KnowledgeServingRouteRow)).all() == []
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(exercise(url))
        with pytest.raises(DBAPIError, match="cannot discard serving acceptance or activation history"):
            command.downgrade(migration_config(), "20261024_0002")
        with isolated.connect() as connection:
            assert connection.execute(select(KnowledgeServingRouteRow.route_version)).scalar_one() == 3
    finally:
        isolated.dispose()
        with root.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        root.dispose()
