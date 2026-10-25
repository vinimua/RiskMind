import asyncio
from datetime import timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from risk_platform.knowledge import milvus
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.ingestion import ingest_pending, rebuild_collection
from risk_platform.knowledge.models import (
    KnowledgeChunkRow, KnowledgeCollectionBuildRow, KnowledgeServingActivationRow, KnowledgeServingRouteRow,
)
from risk_platform.knowledge.serving import (
    MissingServingRouteError, ServingEligibilityError, activate_serving_collection,
    authoritative_snapshot, resolve_serving_route, validate_serving_collection,
    collection_write_guard,
)
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot
from risk_platform.knowledge.serving_contracts import CollectionBuildAcceptance
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSourceRef, KnowledgeDocumentType
from .test_profile_persistence import AsyncFacade, ProfileMilvus, document, sessions, stage
from .test_ingestion import FakeEmbeddingClient


@pytest.fixture(autouse=True)
def inline_milvus_and_sqlite_timezones(monkeypatch):
    async def inline(fn):
        return fn()
    monkeypatch.setattr(milvus, "acall", inline)
    original = AsyncFacade.get

    async def get(self, model, key, **kwargs):
        row = await original(self, model, key, **kwargs)
        # SQLite loses tzinfo; emulate the PostgreSQL timestamptz driver here.
        if isinstance(row, KnowledgeCollectionBuildRow):
            for field in ("completed_at", "validated_at"):
                value = getattr(row, field)
                if value.tzinfo is None:
                    setattr(row, field, value.replace(tzinfo=timezone.utc))
        return row
    monkeypatch.setattr(AsyncFacade, "get", get)


async def candidate(sessions, client, name="candidate", profile=IndexProfile.BASELINE):
    await stage(sessions, document(), profile)
    await rebuild_collection(collection=name, index_profile=profile, session_factory=sessions,
                             milvus_client=client, embedding_client=FakeEmbeddingClient())
    return await validate_serving_collection(collection=name, index_profile=profile,
                                             session_factory=sessions, milvus_client=client)


async def activate(sessions, client, acceptance, key="request-1", expected=0, actor="operator", allow=True):
    async with sessions() as session:
        async with session.begin():
            return await activate_serving_collection(
                session, build_id=acceptance.build_id, actor_id=actor, idempotency_key=key,
                expected_route_version=expected, authorize=lambda *_: allow, milvus_client=client,
            )


def test_snapshot_and_context_contracts():
    snapshot = RetrievalRouteSnapshot(index_profile=IndexProfile.BASELINE, collection="physical", route_version=1)
    with pytest.raises(ValidationError):
        snapshot.collection = "changed"
    for changes in ({"route_version": 0}, {"route_version": True}, {"extra": "unexpected"}, {"collection": " "}):
        with pytest.raises(ValidationError):
            RetrievalRouteSnapshot.model_validate({**snapshot.model_dump(), **changes})
    with pytest.raises(ValidationError):
        KnowledgeContext(context_id="parent", content="Text", supporting_hits=())


def test_context_preserves_child_provenance_and_forbids_extra_fields():
    hit = KnowledgeHit(
        source=KnowledgeSourceRef(document_id="REPORT", document_version="v1", chunk_id="child"),
        document_type=KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT,
        title="Report", section_path="Analysis", content="Child evidence clue", rank=1,
    )
    context = KnowledgeContext(context_id="parent", content="Original context", supporting_hits=(hit,))
    assert context.supporting_hits[0].source == hit.source
    with pytest.raises(ValidationError):
        context.context_id = "changed"
    with pytest.raises(ValidationError):
        KnowledgeContext.model_validate({**context.model_dump(), "evidence": True})


def test_acceptance_contract_cannot_claim_failed_or_mismatched_validation(sessions):
    async def exercise():
        receipt = await candidate(sessions, ProfileMilvus())
        for changes in ({"build_complete": False}, {"validation_status": "INCONCLUSIVE"},
                        {"validation_digest": "0" * 64}, {"extra": "unexpected"}):
            with pytest.raises(ValidationError):
                CollectionBuildAcceptance.model_validate({**receipt.model_dump(), **changes})
        with pytest.raises(ValidationError):
            receipt.build_id = "changed"
    asyncio.run(exercise())


def test_activation_audit_and_route_roll_back_together(sessions):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client)
        async with sessions() as session:
            with pytest.raises(RuntimeError, match="abort"):
                async with session.begin():
                    await activate_serving_collection(
                        session, build_id=receipt.build_id, actor_id="operator", idempotency_key="request-1",
                        expected_route_version=0, authorize=lambda *_: True, milvus_client=client,
                    )
                    raise RuntimeError("abort")
        async with sessions() as session:
            assert list((await session.execute(select(KnowledgeServingActivationRow))).scalars()) == []
            with pytest.raises(MissingServingRouteError):
                await resolve_serving_route(session, IndexProfile.BASELINE)
        assert (await activate(sessions, client, receipt)).route_version == 1
    asyncio.run(exercise())


def test_rebuild_acceptance_is_separate_from_ready_and_does_not_activate(sessions):
    async def exercise():
        client = ProfileMilvus()
        acceptance = await candidate(sessions, client)
        assert acceptance.source_chunk_count == 1
        assert acceptance.authoritative_snapshot_digest == acceptance.validation_digest
        async with sessions() as session:
            row = (await session.execute(select(KnowledgeChunkRow))).scalar_one()
            assert row.ingestion_status == "PENDING"
            with pytest.raises(MissingServingRouteError):
                await resolve_serving_route(session, IndexProfile.BASELINE)
        assert await validate_serving_collection(collection="candidate", index_profile=IndexProfile.BASELINE,
                                                  session_factory=sessions, milvus_client=client) == acceptance
    asyncio.run(exercise())


def test_versioned_activation_idempotency_and_fixed_snapshot(sessions):
    async def exercise():
        client = ProfileMilvus()
        first = await candidate(sessions, client, "first")
        snapshot = await activate(sessions, client, first)
        second = await candidate(sessions, client, "second")
        current = await activate(sessions, client, second, key="request-2", expected=1)
        assert snapshot.collection == "first" and snapshot.route_version == 1
        assert current.collection == "second" and current.route_version == 2
        assert await activate(sessions, client, first) == snapshot  # Receipt, not reactivation.
        async with sessions() as session:
            assert await resolve_serving_route(session, IndexProfile.BASELINE) == current
            assert len(list((await session.execute(select(KnowledgeServingRouteRow))).scalars())) == 1
            history = list((await session.execute(select(KnowledgeServingActivationRow))).scalars())
            assert len(history) == 2 and all(row.activated_by == "operator" for row in history)
        for kwargs in ({"key": "request-1"}, {"key": "request-3", "expected": 0}):
            with pytest.raises((ValueError, ServingEligibilityError)):
                await activate(sessions, client, second, **kwargs)
    asyncio.run(exercise())


@pytest.mark.parametrize("allow", [False, None, 1])
def test_authorization_requires_explicit_true_and_no_route_is_written(sessions, allow):
    async def exercise():
        client = ProfileMilvus()
        acceptance = await candidate(sessions, client)
        with pytest.raises(PermissionError):
            await activate(sessions, client, acceptance, allow=allow)
        async with sessions() as session:
            assert list((await session.execute(select(KnowledgeServingActivationRow))).scalars()) == []
    asyncio.run(exercise())


def test_same_count_source_change_invalidates_acceptance(sessions):
    async def exercise():
        client = ProfileMilvus()
        acceptance = await candidate(sessions, client)
        # Simulate out-of-band corruption; count equality alone cannot detect it.
        async with sessions() as session:
            async with session.begin():
                await session.execute(update(KnowledgeChunkRow).values(content="Changed at same count"))
        with pytest.raises(ServingEligibilityError, match="source snapshot changed"):
            await activate(sessions, client, acceptance)
    asyncio.run(exercise())


def test_status_changes_do_not_change_source_digest(sessions):
    async def exercise():
        await stage(sessions, document(), IndexProfile.BASELINE)
        async with sessions() as session:
            async with session.begin():
                before = await authoritative_snapshot(session, IndexProfile.BASELINE)
                await session.execute(update(KnowledgeChunkRow).values(ingestion_status="READY"))
                assert await authoritative_snapshot(session, IndexProfile.BASELINE) == before
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["missing", "extra", "corrupt"])
def test_incomplete_or_corrupt_materialization_has_no_eligible_record(sessions, failure):
    async def exercise():
        client = ProfileMilvus()
        await stage(sessions, document(), IndexProfile.BASELINE)
        await ingest_pending(collection="ingestion", session_factory=sessions, milvus_client=client,
                             embedding_client=FakeEmbeddingClient())
        row = client.upserts[0][1][0]
        if failure == "missing":
            client.upserts.clear()
        elif failure == "extra":
            client.upserts.append(("ingestion", [{**row, "chunk_id": "extra"}]))
        else:
            client.upserts.append(("ingestion", [{**row, "content": "Corrupt"}]))
        with pytest.raises(ServingEligibilityError):
            await validate_serving_collection(collection="ingestion", index_profile=IndexProfile.BASELINE,
                                              session_factory=sessions, milvus_client=client)
        async with sessions() as session:
            assert list((await session.execute(select(KnowledgeCollectionBuildRow))).scalars()) == []
    asyncio.run(exercise())


def test_activation_rechecks_remote_payload_and_route_changes_roll_back(sessions):
    async def exercise():
        client = ProfileMilvus()
        receipt = await candidate(sessions, client)
        client.upserts[0][1][0]["content"] = "Tampered after validation"
        with pytest.raises(ServingEligibilityError, match="differs"):
            await activate(sessions, client, receipt)
        async with sessions() as session:
            with pytest.raises(MissingServingRouteError):
                await resolve_serving_route(session, IndexProfile.BASELINE)
    asyncio.run(exercise())


def test_profiles_route_independently_and_accepted_targets_are_frozen(sessions):
    async def exercise():
        client = ProfileMilvus()
        baseline = await candidate(sessions, client, "baseline")
        pc = await candidate(sessions, client, "pc", IndexProfile.PARENT_CHILD)
        assert (await activate(sessions, client, baseline)).collection == "baseline"
        assert (await activate(sessions, client, pc, key="pc-request")).collection == "pc"
        before = len(client.upserts)
        assert await rebuild_collection(collection="baseline", session_factory=sessions, milvus_client=client,
                                        embedding_client=FakeEmbeddingClient()) == baseline.source_chunk_count
        assert len(client.upserts) == before
        with pytest.raises(ServingEligibilityError, match="immutable"):
            async with collection_write_guard(sessions, "baseline"):
                raise AssertionError("accepted target must not be writable")
        with pytest.raises(ServingEligibilityError, match="bound"):
            await validate_serving_collection(collection="baseline", index_profile=IndexProfile.PARENT_CHILD,
                                              session_factory=sessions, milvus_client=client)
    asyncio.run(exercise())
