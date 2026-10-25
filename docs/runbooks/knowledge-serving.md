# Stage 6D-1: serving acceptance and activation

This stage defines routing, collection acceptance and controlled activation.
It does not implement dense retrieval, context resolution, a CLI command, an
HTTP endpoint or a strategy registry. Apply migration `20261025_0003` before
using these services. Existing 6C bindings and chunk statuses remain unchanged;
no existing collection automatically becomes eligible or active.

## Contracts

`RetrievalRouteSnapshot` is frozen, forbids extra fields, and contains
`index_profile`, `collection`, and a positive integer `route_version`. Resolve it
once with `resolve_serving_route(session, profile)` at request start. Future
search, source validation and context resolution must receive that same value.
Missing routes raise `MissingServingRouteError`; there is no active-alias fallback.

`CollectionBuildAcceptance` is a frozen successful-acceptance receipt containing
the stable `build_id`, profile, physical collection, source chunk count,
authoritative snapshot digest, matching validation digest, `build_complete=True`,
`validation_status="PASSED"`, completion time and validation time. There is at
most one immutable acceptance per physical collection. Failed validation creates
no eligible record; it must never be reported as PASSED.

`KnowledgeContext` is a frozen contract containing `context_id`, `content` and a
nonempty tuple of supporting `KnowledgeHit` values. Child hits retain provenance.
It is not evidence. The algorithms producing these contexts are deferred to 6D-3.

## Acceptance is independent of READY

`rebuild_collection()` now records acceptance after all writes and its source
count check succeed. It still never changes chunk statuses. Acceptance verifies:

- The collection has a PostgreSQL binding for the requested profile.
- Milvus confirms the physical identity and compatible profile marker.
- Strong-consistency primary-key reads match every authoritative child business
  field, including document identity/version, text, section and page provenance.
- Milvus total count matches the authoritative count, excluding extra entities.

The SHA256 digest is computed from the profile and complete ordered JSON child
payloads, not just IDs or counts. READY, timestamps and embeddings are excluded.
This is materialization integrity validation, not a retrieval-quality benchmark.

`validate_serving_collection()` accepts REBUILD targets only. INGESTION targets
remain continuously writable and cannot receive acceptance or activation.
Successful ingestion creates no acceptance or route. A REBUILD binding alone
does not imply eligibility. Empty complete rebuilds may be explicitly accepted.

Acceptance holds an exclusive profile-source advisory lock and a collection
write lock. Staging takes the shared source lock through commit. Application
Milvus writes use the same collection lock and reject accepted targets. An
accepted physical collection is therefore frozen even before activation.
Repeated successful rebuilds revalidate and return the original count without
rewriting it. If sources changed, use a new physical REBUILD collection.
New documents continue through the same INGESTION target without changing the
current serving snapshot. Only rebuild, validation and explicit activation of a
new REBUILD collection can advance the serving route.

## Explicit activation

```python
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.serving import (
    activate_serving_collection, resolve_serving_route, validate_serving_collection,
)

acceptance = await validate_serving_collection(
    collection="riskmind_baseline_rebuild_001",
    index_profile=IndexProfile.BASELINE,
)

async with sessions() as session:
    async with session.begin():
        receipt = await activate_serving_collection(
            session,
            build_id=acceptance.build_id,
            actor_id=authenticated_actor_id,
            idempotency_key=activation_request_id,
            expected_route_version=0,
            authorize=application_authorizer,
        )

async with sessions() as session:
    request_snapshot = await resolve_serving_route(session, IndexProfile.BASELINE)
```

The required trusted authorizer is synchronous and receives `(actor_id,
"knowledge:serving:activate", index_profile)`. Only an explicit `True` permits
activation. The application must authenticate the actor and supply its policy
implementation; there is no default allow policy or user-facing endpoint here.

Activation verifies successful acceptance, a fresh authoritative source digest
and the Milvus materialization again. It holds the source lock through the
caller's transaction commit. A single current route exists per profile; its
version advances under this lock. The expected-version check prevents lost
updates. The route and an immutable activation-history/idempotency receipt
commit together, recording build ID, actor, collection, version and time.

The initial expected version is zero. Switching targets requires the current
version. Replaying an identical idempotency key returns its original snapshot
without reverting today's route; changed input with the same key is rejected.
Denied or failed activation leaves the prior route intact. Source additions
invalidate old acceptance for a new activation, but do not implicitly switch
or invalidate the already selected immutable serving collection.

PostgreSQL is authoritative for routes and audit history. No Milvus alias is
switched. The accepted 6C external-actor TOCTOU boundary remains: these locks
coordinate platform writers, not arbitrary external Milvus modifications.
Legacy unmarked Baseline adoption remains migration-only compatibility.

## Verification

Run focused contracts/eligibility tests with:

```powershell
python -m pytest tests/knowledge/test_serving.py -q
```

Point `RISK_DATABASE_URL` and `MILVUS_URI` at disposable servers, set
`RUN_POSTGRES_INTEGRATION=1` and `RUN_INGESTION_INTEGRATION=1`, then run
`tests/knowledge/test_serving_postgres.py`. It migrates 0002 to 0003 in a unique
temporary schema, tests real Milvus payload acceptance and activation CAS, and
proves source writers wait through activation commit. It uses deterministic
test embeddings, never an external embedding API. Only its generated schema
and collection names are removed. Migration downgrade refuses to discard
acceptance or activation history.
