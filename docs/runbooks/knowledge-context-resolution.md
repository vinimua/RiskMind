# Stage 6D-3: context resolution

`resolve_knowledge_contexts(DenseChildRetrievalResult)` returns a frozen
`ContextResolutionResult(snapshot, contexts)`. It preserves the original route
snapshot value. It never resolves the active route, searches Milvus, embeds a
query, checks READY or compares today's entire source set with the serving build.
The 6D-2 implementation and contracts remain unchanged.

```python
from risk_platform.knowledge.retrieval import retrieve_dense_children
from risk_platform.knowledge.context_resolution import resolve_knowledge_contexts

children = await retrieve_dense_children(request, index_profile=profile)
resolved = await resolve_knowledge_contexts(children)
# resolved.snapshot is the route used by the original search.
# resolved.contexts contains KnowledgeContext values, never Evidence objects.
```

The resolver consumes the internal result from the trusted 6D-2 service. It
copies/revalidates the result and checks child identity, version and hit payload
against PostgreSQL under the snapshot profile. Duplicate child IDs or invalid
rank order fail the whole request. Valid ranks remain unchanged in supporting
hits. Database/dependency failures propagate; incomplete lineage never becomes
an empty or partially successful context result.

Baseline uses one batch read of authoritative children and returns one context
per hit in rank order: `context_id = chunk_id`, authoritative Child content, and
one supporting hit. It does not read Parent or Link tables.

Parent-Child uses three batch reads: children, their profile-scoped Links, and
the profile-scoped Parents. Every hit must have exactly one link and its Parent
must exist. Parent metadata must be valid, match the Child's document identity,
version, type, title, section, model and source URI, and contain the Child's page
span. Missing or inconsistent lineage raises `ContextResolutionIntegrityError`.
No fallback to Child context occurs for a broken Parent-Child mapping.

Iterating Child hits in their original rank order determines Parent order by
first occurrence, equivalent to best Child rank. A Parent is returned once, and
all its matched Child hits are appended to `supporting_hits` in original rank
order. No score aggregation, child-count boost, parent reranking, or truncation
is applied. Links' `child_ordinal` values describe source order, not retrieval
rank; a retrieval subset need not have contiguous source ordinals.

For example, Child ranks `C01→P2, C08→P1, C03→P2, C11→P3` produce Parents
`P2, P1, P3` with supporting ranks `[1,3], [2], [4]`. Parent content comes directly
from authoritative `KnowledgeContextRow.content`; it is never reconstructed by
concatenating overlapping Child text. KnowledgeContext is not KnowledgeHit or
Evidence; provenance remains attached to supporting Child hits.

Newly staged sources and a later activation do not redirect an existing result.
Because canonical Child/Parent/Link objects are immutable, the original IDs can
still resolve against PostgreSQL under the original profile. Resolution does
not replace the request snapshot with the current active route. Empty retrieval
results yield empty contexts with the same snapshot, without accessing PG.

Run focused tests with
`python -m pytest tests/knowledge/test_context_resolution.py -q`. Enable both
`RUN_POSTGRES_INTEGRATION=1` and `RUN_MILVUS_INTEGRATION=1` and configure disposable
servers for `test_context_resolution_integration.py`. It runs the complete
rebuild/activate/dense-search/context flow for both profiles, then activates v2
and proves the prior result still resolves identically. It uses deterministic
embeddings, a unique schema and generated physical collections; no external
embedding provider is called. This verifies pipeline integrity, not semantic
quality. Benchmarks remain Stage 6E.

Existing P2 boundaries remain: cosine domain checking is not changed from 6D-2,
serving full-validation lock cost, the external Milvus preflight/bind TOCTOU,
and migration-period legacy Baseline adoption.
