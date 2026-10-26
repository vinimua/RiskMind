"""Real PG/Milvus plumbing only; fixed vectors cannot establish RAG quality."""
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from pymilvus import MilvusClient
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.knowledge import milvus
from risk_platform.knowledge.benchmark import BenchmarkDataset, run_benchmark
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import rebuild_collection
from risk_platform.knowledge.serving import validate_serving_collection
from .test_profile_migration import migration_config
from .test_profile_persistence import stage
from .test_retrieval_integration import FixedEmbedding
from .test_serving import activate

pytestmark = pytest.mark.skipif(
    any(os.environ.get(name) != "1" for name in ("RUN_POSTGRES_INTEGRATION", "RUN_MILVUS_INTEGRATION")),
    reason="requires disposable PostgreSQL and Milvus",
)


async def exercise(url):
    dataset = BenchmarkDataset.model_validate_json(
        (Path(__file__).parents[1] / "fixtures/knowledge_benchmark_v1.json").read_text(encoding="utf-8"))
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = await milvus.acall(lambda: MilvusClient(uri=os.environ["MILVUS_URI"]))
    names = []
    try:
        for profile in IndexProfile:
            name = "riskmind_bench_" + uuid4().hex
            names.append(name)
            for doc in dataset.documents:
                await stage(sessions, doc, profile)
            await rebuild_collection(collection=name, index_profile=profile, session_factory=sessions,
                                     milvus_client=client, embedding_client=FixedEmbedding())
            receipt = await validate_serving_collection(collection=name, index_profile=profile,
                                                        session_factory=sessions, milvus_client=client)
            await activate(sessions, client, receipt, key=profile.value)
        result = await run_benchmark(dataset, repeats=1, session_factory=sessions,
                                     milvus_client=client, embedding_client=FixedEmbedding())
        assert len(result["rows"]) == 2 * len(dataset.questions)
        assert {row["retrieved"]["snapshot"]["collection"] for row in result["rows"]} == set(names)
        for row in result["rows"]:
            assert 0 <= row["metrics"]["context_relevant_quote_recall"] <= 1
            assert row["metrics"]["context_tokens"] is None
            assert row["retrieved"]["snapshot"] == row["resolved"]["snapshot"]
    finally:
        def cleanup():
            for name in names:
                if client.has_collection(name):
                    client.drop_collection(name)
            client.close()
        await milvus.acall(cleanup)
        await engine.dispose()


def test_real_benchmark_pipeline_without_semantic_quality_claims(monkeypatch):
    schema = "test_bench_" + uuid4().hex
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
