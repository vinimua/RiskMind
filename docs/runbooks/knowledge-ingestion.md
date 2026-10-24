# Knowledge ingestion CLI

Install the backend with its database and Milvus dependencies, then apply the
Alembic migration before using the CLI:

```powershell
python -m pip install -e "./backend[database,knowledge]"
cd backend
alembic -c alembic.ini upgrade head
cd ..
```

Set `RISK_DATABASE_URL`,
`SILICONFLOW_API_KEY`, and, if needed, `MILVUS_URI` in the environment. Keep API
keys out of source files.

Run these commands from the project root:

```powershell
python -m risk_platform.knowledge.cli stage docs/runbooks/credit_v3_model_development_report_fixture.md `
  --document-id MODEL-DEV-credit_v3 --document-version v1 `
  --document-type MODEL_DEVELOPMENT_REPORT `
  --title "credit_v3 模型开发报告（Synthetic Fixture）" `
  --source-uri knowledge://models/credit_v3/v1 `
  --model-id credit_v3

python -m risk_platform.knowledge.cli resume --collection riskmind_knowledge_v003
python -m risk_platform.knowledge.cli rebuild --collection riskmind_knowledge_v004
```

`stage` accepts canonical Markdown with a first H1 equal to `--title`. It stages
one immutable document version; repeating the same input is a no-op. This CLI
does not parse PDFs or assign PDF page numbers. A PDF parser must supply the
per-page Markdown through the document contract separately.

`resume` handles only `PENDING` chunks. It marks each batch `READY` after Milvus
upsert and flush; rerun it after a failure.

`rebuild` reads both `PENDING` and `READY`, re-embeds them, and upserts them to
the specified physical collection. It never changes PostgreSQL statuses. Use
a **new empty physical collection** for a complete rebuild: upsert does not
remove stale rows already present in an existing collection. An interrupted
rebuild can be rerun against that same new collection. If PostgreSQL gains
chunks while rebuild is running, the command reports an error; rerun it to
include the new chunks. Neither command switches the active Milvus alias.

## Stage 6C: index profiles

Apply migration `20261024_0002` before running the updated code. It assigns all
existing chunks to `baseline-v1`, preserving IDs, business fields and statuses.
The chunk primary key is now `(chunk_id, index_profile)`. Parent context and
explicit child links live only in PostgreSQL; only search chunks are embedded
and written into Milvus. No retrieval or active-alias changes are included.

The two frozen profiles are `IndexProfile.BASELINE` (`baseline-v1`) and
`IndexProfile.PARENT_CHILD` (`parent-child-v1`). Changes to build parameters or
semantics require a new scheme/profile and a subsequent explicit migration.
There is no configuration-driven strategy selector. The CLI remains Baseline.
Application callers can stage a profile explicitly:

```python
from risk_platform.knowledge.indexing import ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.repository import stage_prepared
from risk_platform.knowledge.ingestion import ingest_pending, rebuild_collection

prepared = ParentChildIndexBuildStrategy().build(document)
async with sessions() as session:
    async with session.begin():
        await stage_prepared(
            session, document, prepared,
            index_profile=IndexProfile.PARENT_CHILD,
        )

await ingest_pending(
    collection="riskmind_pc_v1_ingestion",
    index_profile=IndexProfile.PARENT_CHILD,
)
await rebuild_collection(
    collection="riskmind_pc_v1_rebuild_001",
    index_profile=IndexProfile.PARENT_CHILD,
)
```

`stage_prepared` validates the supplied artifact against the frozen profile and
stages the canonical document source, build digest, search chunks, contexts and
links in the caller's single transaction. The same document ID/version cannot
change across profiles. The digest includes artifact order. Identical retries
return zero without regressing READY. Existing legacy documents enroll when
their supplied source reproduces the persisted Baseline business fields; their
original raw source cannot be recovered from historical chunks. Once enrolled,
the complete supplied canonical source is immutable.

Every physical collection is durably bound to a profile and a write purpose
before external writes. A profile has one INGESTION collection and may have
multiple REBUILD collections. Bindings cannot be repurposed implicitly. New
Milvus collections also carry the profile in their schema description; vector
fields and indexes remain unchanged. Parent-Child refuses unmarked or differently
marked existing collections. Alias names are rejected before writes.

READY means successful upsert/flush to the profile's INGESTION target. It does
not imply presence in any other collection. Rebuild reads that profile's stored
PENDING and READY children, never reruns chunking, never updates statuses, and
detects changes in that profile's source count. Rerun an interrupted rebuild
against the same dedicated target. Use a fresh empty target for a complete
rebuild; upsert does not delete stale entities. Promotion, target retirement and
alias switching are outside Stage 6C.

For an upgrade with existing READY Baseline rows, the first `resume` binding
must name the actual historical ingestion collection. PostgreSQL did not
previously record this name and cannot infer it. An unmarked existing collection
may be adopted only as Baseline. Use `rebuild` for any new collection. Do not
remove production binding rows to bypass this rule; removing bindings or changing
Milvus descriptions requires a separately reviewed recovery procedure.

Downgrade refuses to discard enrolled sources, profile builds or collection
ownership. Export and explicitly retire these objects before a downgrade.
`RUN_POSTGRES_INTEGRATION=1` enables an isolated-schema test of migration, legacy
row preservation, concurrent staging and conflicting collection ownership;
the existing Milvus/ingestion integration switches remain available.
