import asyncio
import os
from uuid import uuid4

import pytest

from risk_platform.knowledge import milvus
from risk_platform.knowledge.indexing.profiles import IndexProfile
from .test_milvus import FakeMilvusClient


def test_new_collection_carries_profile_identity_without_changing_vector_fields():
    baseline, pc = FakeMilvusClient(), FakeMilvusClient()
    milvus.ensure_collection(baseline, "baseline", index_profile=IndexProfile.BASELINE)
    milvus.ensure_collection(pc, "pc", index_profile=IndexProfile.PARENT_CHILD)
    assert baseline.schema.fields == pc.schema.fields
    assert pc.schema_options["description"] == "riskmind:index-profile:parent-child-v1"
    assert baseline.schema_options["description"] == "riskmind:index-profile:baseline-v1"
    milvus.ensure_collection(pc, "pc", index_profile=IndexProfile.PARENT_CHILD)
    with pytest.raises(ValueError, match="another or unknown index profile"):
        milvus.ensure_collection(pc, "pc", index_profile=IndexProfile.BASELINE)


def test_legacy_collection_can_only_be_adopted_as_baseline():
    client = FakeMilvusClient(collection_exists=True)
    milvus.ensure_collection(client, "legacy", index_profile=IndexProfile.BASELINE)
    with pytest.raises(ValueError, match="another or unknown index profile"):
        milvus.ensure_collection(client, "legacy", index_profile=IndexProfile.PARENT_CHILD)


def test_non_active_alias_cannot_bypass_profile_binding(monkeypatch):
    client = FakeMilvusClient(collection_exists=True)
    monkeypatch.setattr(client, "describe_collection", lambda _: {
        "collection_name": "physical", "description": "riskmind:index-profile:parent-child-v1",
    })
    with pytest.raises(ValueError, match="not an alias"):
        milvus.ensure_collection(client, "some_alias", index_profile=IndexProfile.PARENT_CHILD)
    assert client.upserts == []


@pytest.mark.skipif(
    os.environ.get("RUN_MILVUS_INTEGRATION") != "1",
    reason="set RUN_MILVUS_INTEGRATION=1 for real profile collection identity and idempotent upsert",
)
def test_real_milvus_profile_marker_and_child_upsert():
    suffix = uuid4().hex
    collection = f"riskmind_pc_profile_test_{suffix}"

    def exercise():
        client = milvus.get_client()
        try:
            milvus.ensure_collection(client, collection, index_profile=IndexProfile.PARENT_CHILD)
            assert client.describe_collection(collection)["description"] == "riskmind:index-profile:parent-child-v1"
            row = dict(
                chunk_id=f"pc-child-{suffix}", document_id="PROFILE-TEST", document_version="v1",
                document_type="MODEL_DEVELOPMENT_REPORT", title="Profile test", section_path="Analysis",
                content="Evidence.", model_id=None, source_uri="knowledge://profile-test",
                page_start=None, page_end=None, dense_vector=[1.0] + [0.0] * 1023,
            )
            for _ in range(2):
                milvus.ensure_collection(client, collection, index_profile=IndexProfile.PARENT_CHILD)
                milvus.upsert_chunks(client, [row], collection)
                milvus.flush(client, collection)
            counts = client.query(
                collection, filter=f'chunk_id == "pc-child-{suffix}"',
                output_fields=["count(*)"], consistency_level="Strong",
            )
            assert counts[0]["count(*)"] == 1
            with pytest.raises(ValueError, match="another or unknown index profile"):
                milvus.ensure_collection(client, collection, index_profile=IndexProfile.BASELINE)
        finally:
            if client.has_collection(collection):
                client.drop_collection(collection)
    asyncio.run(milvus.acall(exercise))
