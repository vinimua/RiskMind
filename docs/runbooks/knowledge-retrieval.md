# Stage 6D-2: dense child retrieval

`retrieve_dense_children(request, index_profile=...)` resolves the profile's
serving route exactly once. It returns `DenseChildRetrievalResult(snapshot,
hits)`, where each `RetrievedKnowledgeHit` contains a `KnowledgeHit` and finite
cosine `score`. These output envelopes are frozen and forbid extra fields;
the existing KnowledgeHit contract is unchanged. No Parent lookup, context
resolution, deduplication, reranking or hybrid retrieval is performed.

```python
from risk_platform.knowledge.contracts import KnowledgeSearchRequest
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import retrieve_dense_children

result = await retrieve_dense_children(
    KnowledgeSearchRequest(query="credit_v3 historical findings", top_k=5),
    index_profile=IndexProfile.PARENT_CHILD,
)
# Preserve result.snapshot alongside the hits for the later resolution stage.
```

The request is copied and revalidated before any I/O. Missing serving routes
fail before embedding. The service embeds the query with the same existing
embedding client used by indexing, verifies vector dimension/finiteness and
rejects zero vectors. It explicitly searches the snapshot's physical collection
using Dense COSINE plus existing metadata filters. It never falls back to the
legacy active alias, never reloads the route mid-request, and never creates a
collection. Physical identity/profile checks run before search.

After search, the service batch-reads PostgreSQL children with both profile and
chunk IDs. Every Milvus business field must exactly match the authoritative
record, including identity/version, section, content, model, URI and page span.
Requested filters are independently checked against PostgreSQL. Returned hits
are constructed from PG values, ranked contiguously in search order. Equal
scores retain Milvus order; no parent-level score aggregation is applied.

Unknown/profile-mismatched children, altered metadata, duplicate IDs, non-finite
scores, ascending cosine results, filter violations and over-top-k responses
raise `ChildRetrievalIntegrityError`. Dependency errors propagate. Any invalid
candidate fails the whole request; there is no partial-success or silent-drop
path. Empty results from a valid serving collection are legitimate empty hits.
Callers must not interpret lookup/dependency/integrity failures as evidence or
successful empty retrieval. The existing KnowledgeRetrievalResult contract is
not changed or repurposed by this stage.

READY is intentionally not a retrieval gate: it refers to the INGESTION target,
whereas serving reads an independently validated REBUILD snapshot. Likewise,
retrieval does not compare the serving build against today's entire source
digest. New staged documents do not invalidate the already activated immutable
collection, nor become visible without rebuild/validate/activate. A route switch
during embedding does not redirect an in-flight request. Prior physical serving
collections must remain available for outstanding snapshots; retirement is not
implemented here. The existing external-Milvus-actor and legacy-adoption P2
boundaries remain unchanged.

Run focused tests with `python -m pytest tests/knowledge/test_retrieval.py -q`.
Enable `RUN_POSTGRES_INTEGRATION=1` and `RUN_MILVUS_INTEGRATION=1`, point
`RISK_DATABASE_URL` and `MILVUS_URI` at disposable servers, and run
`tests/knowledge/test_retrieval_integration.py` for real routed dense searches
and PG validation under both profiles. The integration test uses a unique
temporary schema and generated collections, deterministic nonzero embeddings,
and never calls an external embedding provider. It verifies plumbing/integrity,
not semantic relevance or retrieval quality; benchmarks remain Stage 6E.
