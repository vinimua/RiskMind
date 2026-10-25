"""Deterministic serving acceptance and activation; no search or resolution."""

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from pymilvus import MilvusClient

from risk_platform.db.database import get_session_factory
from risk_platform.knowledge import milvus
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import (
    KnowledgeCollectionBuildRow, KnowledgeCollectionRow,
    KnowledgeServingActivationRow, KnowledgeServingRouteRow,
)
from risk_platform.knowledge.repository import list_rebuild_chunks, lock_profile_source
from risk_platform.knowledge.serving_contracts import CollectionBuildAcceptance, RetrievalRouteSnapshot


class ServingEligibilityError(ValueError):
    pass


class MissingServingRouteError(LookupError):
    pass


async def _lock(session, key: str):
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


@asynccontextmanager
async def collection_write_guard(sessions, collection: str):
    """Serialize writes with acceptance; an accepted physical target is frozen."""
    async with sessions() as session:
        async with session.begin():
            await _lock(session, f"riskmind:knowledge-materialization:{collection}")
            accepted = await get_accepted_build(session, collection)
            if accepted is not None:
                raise ServingEligibilityError("accepted collection is immutable; use a new physical collection")
            yield


async def get_accepted_build(session, collection: str):
    return (await session.execute(select(KnowledgeCollectionBuildRow).where(
        KnowledgeCollectionBuildRow.collection == collection,
    ))).scalars().first()


def _payload(row) -> dict:
    return {field: getattr(row, field) for field in milvus.OUTPUT_FIELDS}


def _digest_row(digest, payload):
    digest.update(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


async def authoritative_snapshot(session, profile: IndexProfile) -> tuple[int, str]:
    """Hash the ordered complete business payload, excluding ingestion status."""
    digest = hashlib.sha256(profile.value.encode("utf-8") + b"\n")
    count, cursor = 0, None
    while True:
        rows = await list_rebuild_chunks(session, limit=64, after_chunk_id=cursor, index_profile=profile)
        if not rows:
            return count, digest.hexdigest()
        for row in rows:
            _digest_row(digest, _payload(row))
        count += len(rows)
        cursor = rows[-1].chunk_id


async def _validate_materialized(session, client, collection, profile):
    def check():
        if not client.has_collection(collection):
            raise ServingEligibilityError("serving candidate collection does not exist")
        milvus.validate_collection_for_profile(client, collection, profile)
        return client.query(collection, filter="", output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"]

    actual_count = await milvus.acall(check)
    digest = hashlib.sha256(profile.value.encode("utf-8") + b"\n")
    count, cursor = 0, None
    while True:
        rows = await list_rebuild_chunks(session, limit=64, after_chunk_id=cursor, index_profile=profile)
        if not rows:
            break
        expected = [_payload(row) for row in rows]

        def read_batch():
            return client.get(collection, ids=[row.chunk_id for row in rows],
                              output_fields=milvus.OUTPUT_FIELDS, consistency_level="Strong")

        actual = await milvus.acall(read_batch)
        by_id = {row["chunk_id"]: row for row in actual}
        if len(by_id) != len(expected):
            raise ServingEligibilityError("materialized collection has missing or duplicate children")
        for payload in expected:
            if by_id.get(payload["chunk_id"]) != payload:
                raise ServingEligibilityError("materialized child differs from authoritative payload")
            _digest_row(digest, payload)
        count += len(rows)
        cursor = rows[-1].chunk_id
    if actual_count != count:
        raise ServingEligibilityError("materialized collection has extra or missing children")
    return count, digest.hexdigest()


def _acceptance(row):
    return CollectionBuildAcceptance.model_validate({
        name: getattr(row, name) for name in CollectionBuildAcceptance.model_fields
    })


async def validate_serving_collection(
    *, collection: str, index_profile: IndexProfile,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    milvus_client: MilvusClient | None = None,
) -> CollectionBuildAcceptance:
    """Explicitly accept a complete REBUILD target; never activate it.

    Only successful acceptance is persisted. Failed/inconclusive validation has
    no eligible record. Identical repeats return the original immutable receipt.
    """
    profile = IndexProfile(index_profile)
    sessions = session_factory or get_session_factory()
    client = milvus_client or await milvus.acall(milvus.get_client)
    async with sessions() as session:
        async with session.begin():
            await lock_profile_source(session, profile)
            await _lock(session, f"riskmind:knowledge-materialization:{collection}")
            binding = await session.get(KnowledgeCollectionRow, collection)
            if binding is None or binding.index_profile != profile.value:
                raise ServingEligibilityError("collection must be bound to the requested profile")
            if binding.purpose != "REBUILD":
                raise ServingEligibilityError("serving acceptance requires a REBUILD target; INGESTION remains writable")
            count, digest = await _validate_materialized(session, client, collection, profile)
            build_id = "collection-build-" + hashlib.sha256(
                json.dumps([profile.value, collection, digest]).encode("utf-8")
            ).hexdigest()
            existing = await session.get(KnowledgeCollectionBuildRow, build_id)
            if existing is not None:
                return _acceptance(existing)
            # A frozen target cannot silently acquire a different acceptance.
            previous = (await session.execute(select(KnowledgeCollectionBuildRow).where(
                KnowledgeCollectionBuildRow.collection == collection,
            ))).scalars().first()
            if previous is not None:
                raise ServingEligibilityError("collection already accepted against another source snapshot")
            now = datetime.now(timezone.utc)
            row = KnowledgeCollectionBuildRow(
                build_id=build_id, collection=collection, index_profile=profile.value,
                source_chunk_count=count, authoritative_snapshot_digest=digest, validation_digest=digest,
                build_complete=True, validation_status="PASSED", completed_at=now, validated_at=now,
            )
            session.add(row)
            await session.flush()
            return _acceptance(row)


async def resolve_serving_route(session: AsyncSession, index_profile: IndexProfile) -> RetrievalRouteSnapshot:
    profile = IndexProfile(index_profile)
    row = await session.get(KnowledgeServingRouteRow, profile.value, populate_existing=True)
    if row is None:
        raise MissingServingRouteError(f"no serving route configured for {profile.value}")
    return RetrievalRouteSnapshot(index_profile=profile, collection=row.collection, route_version=row.route_version)


async def activate_serving_collection(
    session: AsyncSession, *, build_id: str, actor_id: str, idempotency_key: str,
    expected_route_version: int,
    authorize: Callable[[str, str, IndexProfile], bool], milvus_client: MilvusClient | None = None,
) -> RetrievalRouteSnapshot:
    """Caller owns transaction. Trusted authorizer must explicitly return True.

    Snapshot receipt, history and current route commit atomically. Reusing a key
    with different input fails; replay returns the original version, not today's.
    """
    for value in (actor_id, idempotency_key, build_id):
        if not value.strip() or len(value) > 128:
            raise ValueError("activation identifiers must be nonblank and at most 128 characters")
    if type(expected_route_version) is not int or expected_route_version < 0:
        raise ValueError("expected_route_version must be a non-negative integer")
    build = await session.get(KnowledgeCollectionBuildRow, build_id)
    if build is None:
        raise ServingEligibilityError("successful collection acceptance is required")
    profile = IndexProfile(build.index_profile)
    if authorize(actor_id, "knowledge:serving:activate", profile) is not True:
        raise PermissionError("actor is not authorized to activate this index profile")
    await _lock(session, f"riskmind:serving-activation-key:{idempotency_key}")
    await lock_profile_source(session, profile)
    binding = await session.get(KnowledgeCollectionRow, build.collection)
    if binding is None or binding.index_profile != profile.value or binding.purpose != "REBUILD":
        raise ServingEligibilityError("serving activation requires a REBUILD target")
    receipt = await session.get(KnowledgeServingActivationRow, idempotency_key)
    if receipt is not None:
        if (receipt.build_id, receipt.activated_by, receipt.route_version - 1) != (
            build_id, actor_id, expected_route_version,
        ):
            raise ValueError("activation idempotency key already used with different input")
        return RetrievalRouteSnapshot(index_profile=profile, collection=receipt.collection, route_version=receipt.route_version)
    route = await session.get(KnowledgeServingRouteRow, profile.value, populate_existing=True)
    if (route.route_version if route else 0) != expected_route_version:
        raise ServingEligibilityError("serving route version changed; refresh before activating")
    if not build.build_complete or build.validation_status != "PASSED":
        raise ServingEligibilityError("collection build is not eligible")
    count, digest = await authoritative_snapshot(session, profile)
    if (count, digest) != (build.source_chunk_count, build.authoritative_snapshot_digest):
        raise ServingEligibilityError("authoritative source snapshot changed after validation")
    await _lock(session, f"riskmind:knowledge-materialization:{build.collection}")
    client = milvus_client or await milvus.acall(milvus.get_client)
    if await _validate_materialized(session, client, build.collection, profile) != (count, digest):
        raise ServingEligibilityError("collection validation no longer matches acceptance")
    version, now = expected_route_version + 1, datetime.now(timezone.utc)
    values = dict(index_profile=profile.value, collection=build.collection, build_id=build_id,
                  route_version=version, activated_at=now, activated_by=actor_id)
    session.add(KnowledgeServingActivationRow(idempotency_key=idempotency_key, **values))
    await session.flush()
    if route is None:
        session.add(KnowledgeServingRouteRow(**values))
    else:
        for key, value in values.items():
            setattr(route, key, value)
    await session.flush()
    return RetrievalRouteSnapshot(index_profile=profile, collection=build.collection, route_version=version)
