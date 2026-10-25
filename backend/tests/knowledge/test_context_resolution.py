import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, update

from risk_platform.knowledge.context_resolution import ContextResolutionIntegrityError, resolve_knowledge_contexts
from risk_platform.knowledge.chunking import DocumentPage
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSourceRef
from risk_platform.knowledge.indexing import ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeContextLinkRow, KnowledgeContextRow
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult, RetrievedKnowledgeHit
from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot
from .test_profile_persistence import AsyncFacade, document, sessions, stage


async def retrieval_result(sessions, profile, ids=None):
    async with sessions() as session:
        rows = list((await session.execute(select(KnowledgeChunkRow).where(
            KnowledgeChunkRow.index_profile == profile.value,
        ).order_by(KnowledgeChunkRow.chunk_id))).scalars())
    children = {row.chunk_id: row for row in rows}
    ordered = [children[key] for key in ids] if ids is not None else rows
    return DenseChildRetrievalResult(
        snapshot=RetrievalRouteSnapshot(index_profile=profile, collection="original_physical_v1", route_version=7),
        hits=tuple(RetrievedKnowledgeHit(
            hit=KnowledgeHit(
                source=KnowledgeSourceRef(document_id=row.document_id, document_version=row.document_version,
                                          chunk_id=row.chunk_id),
                document_type=row.document_type, title=row.title, section_path=row.section_path,
                model_id=row.model_id, content=row.content, rank=rank,
            ), score=0.9 - rank * 0.01,
        ) for rank, row in enumerate(ordered, 1)),
    )


def parent_document():
    return document("unused").model_copy(update={"pages": (
        DocumentPage(
            content="# Report\n## P1\n" + "第一证据。" * 180 + "\n## P2\n" + "第二证据。" * 180
            + "\n## P3\n" + "第三证据。" * 180,
        ),
    )})


def test_baseline_child_contexts_preserve_order_rank_and_snapshot(sessions, monkeypatch):
    async def exercise():
        await stage(sessions, document("First"), IndexProfile.BASELINE)
        await stage(sessions, document("Second", version="v2"), IndexProfile.BASELINE)
        original = await retrieval_result(sessions, IndexProfile.BASELINE)
        ids = [item.hit.source.chunk_id for item in reversed(original.hits)]
        original = await retrieval_result(sessions, IndexProfile.BASELINE, ids)
        queries = []
        execute = AsyncFacade.execute

        async def track(self, statement, parameters=None):
            queries.append(str(statement))
            return await execute(self, statement, parameters)
        monkeypatch.setattr(AsyncFacade, "execute", track)
        resolved = await resolve_knowledge_contexts(original, session_factory=sessions)
        assert resolved.snapshot == original.snapshot
        assert [context.context_id for context in resolved.contexts] == ids
        assert [context.content for context in resolved.contexts] == [item.hit.content for item in original.hits]
        assert [context.supporting_hits for context in resolved.contexts] == [(item.hit,) for item in original.hits]
        assert len(queries) == 1 and "knowledge_chunks" in queries[0]
        with pytest.raises(ValidationError):
            resolved.contexts = ()
    asyncio.run(exercise())


def test_parent_best_rank_dedup_noncontiguous_ordinals_and_original_content(sessions, monkeypatch):
    async def exercise():
        doc = parent_document()
        prepared = ParentChildIndexBuildStrategy().build(doc)
        await stage(sessions, doc, IndexProfile.PARENT_CHILD)
        parents = prepared.context_units
        grouped = {parent.context_id: [link for link in prepared.context_links if link.context_id == parent.context_id]
                   for parent in parents}
        p1, p2, p3 = parents
        assert grouped[p2.context_id][-1].child_ordinal > 1
        selected = [grouped[p2.context_id][0].chunk_id, grouped[p1.context_id][0].chunk_id,
                    grouped[p2.context_id][-1].chunk_id, grouped[p3.context_id][0].chunk_id]
        original = await retrieval_result(sessions, IndexProfile.PARENT_CHILD, selected)
        queries = []
        execute = AsyncFacade.execute

        async def track(self, statement, parameters=None):
            queries.append(str(statement))
            return await execute(self, statement, parameters)
        monkeypatch.setattr(AsyncFacade, "execute", track)
        resolved = await resolve_knowledge_contexts(original, session_factory=sessions)
        assert resolved.snapshot == original.snapshot
        assert [context.context_id for context in resolved.contexts] == [p2.context_id, p1.context_id, p3.context_id]
        assert [[hit.rank for hit in context.supporting_hits] for context in resolved.contexts] == [[1, 3], [2], [4]]
        assert [context.content for context in resolved.contexts] == [p2.content, p1.content, p3.content]
        assert resolved.contexts[0].content != "".join(hit.content for hit in resolved.contexts[0].supporting_hits)
        assert len(queries) == 3 and not any("serving" in query for query in queries)
    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["missing-child", "wrong-profile", "payload", "source", "duplicate", "rank"])
def test_invalid_child_hits_fail_closed(sessions, fault):
    async def exercise():
        await stage(sessions, document(), IndexProfile.BASELINE)
        original = await retrieval_result(sessions, IndexProfile.BASELINE)
        data = original.model_dump()
        if fault == "missing-child":
            data["hits"][0]["hit"]["source"]["chunk_id"] = "missing"
        elif fault == "wrong-profile":
            data["snapshot"]["index_profile"] = IndexProfile.PARENT_CHILD
        elif fault == "payload":
            data["hits"][0]["hit"]["content"] = "Changed"
        elif fault == "source":
            data["hits"][0]["hit"]["source"]["document_version"] = "wrong"
        elif fault == "duplicate":
            data["hits"] = (data["hits"][0], data["hits"][0])
        else:
            data["hits"][0]["hit"]["rank"] = 2
        with pytest.raises(ContextResolutionIntegrityError):
            await resolve_knowledge_contexts(DenseChildRetrievalResult.model_validate(data), session_factory=sessions)
    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["missing-link", "wrong-parent", "uri", "section", "page"])
def test_invalid_parent_mapping_or_provenance_fails_closed(sessions, fault):
    async def exercise():
        await stage(sessions, parent_document(), IndexProfile.PARENT_CHILD)
        original = await retrieval_result(sessions, IndexProfile.PARENT_CHILD)
        async with sessions() as session:
            async with session.begin():
                if fault == "missing-link":
                    await session.execute(delete(KnowledgeContextLinkRow).where(
                        KnowledgeContextLinkRow.chunk_id == original.hits[0].hit.source.chunk_id,
                    ))
                elif fault == "wrong-parent":
                    parents = list((await session.execute(select(KnowledgeContextRow))).scalars())
                    wrong = next(parent for parent in parents if parent.section_path != original.hits[0].hit.section_path)
                    await session.execute(update(KnowledgeContextLinkRow).where(
                        KnowledgeContextLinkRow.chunk_id == original.hits[0].hit.source.chunk_id,
                    ).values(context_id=wrong.context_id, child_ordinal=999))
                else:
                    values = {"uri": {"source_uri": "knowledge://wrong"}, "section": {"section_path": "Wrong"},
                              "page": {"page_start": 2, "page_end": 3}}[fault]
                    await session.execute(update(KnowledgeContextRow).values(**values))
        with pytest.raises(ContextResolutionIntegrityError):
            await resolve_knowledge_contexts(original, session_factory=sessions)
    asyncio.run(exercise())


def test_empty_result_requires_no_database_and_preserves_snapshot():
    original = DenseChildRetrievalResult(snapshot=RetrievalRouteSnapshot(
        index_profile=IndexProfile.PARENT_CHILD, collection="old_collection", route_version=3), hits=())

    def forbidden():
        raise AssertionError("empty resolution must not query the database")
    result = asyncio.run(resolve_knowledge_contexts(original, session_factory=forbidden))
    assert result.contexts == () and result.snapshot == original.snapshot


def test_new_authoritative_source_does_not_invalidate_old_contexts(sessions):
    async def exercise():
        await stage(sessions, parent_document(), IndexProfile.PARENT_CHILD)
        original = await retrieval_result(sessions, IndexProfile.PARENT_CHILD)
        before = await resolve_knowledge_contexts(original, session_factory=sessions)
        await stage(sessions, document("New source", version="v2"), IndexProfile.PARENT_CHILD)
        assert await resolve_knowledge_contexts(original, session_factory=sessions) == before
    asyncio.run(exercise())


def test_missing_parent_fails_without_partial_contexts(sessions, monkeypatch):
    async def exercise():
        await stage(sessions, parent_document(), IndexProfile.PARENT_CHILD)
        original = await retrieval_result(sessions, IndexProfile.PARENT_CHILD)
        execute = AsyncFacade.execute

        class Empty:
            def scalars(self):
                return []

        async def missing(self, statement, parameters=None):
            if "FROM knowledge_contexts" in str(statement):
                return Empty()
            return await execute(self, statement, parameters)
        monkeypatch.setattr(AsyncFacade, "execute", missing)
        with pytest.raises(ContextResolutionIntegrityError, match="parent is missing"):
            await resolve_knowledge_contexts(original, session_factory=sessions)
    asyncio.run(exercise())


@pytest.mark.parametrize("corrupt", [False, True])
def test_parent_page_span_contains_children(sessions, corrupt):
    async def exercise():
        doc = document().model_copy(update={"pages": (
            DocumentPage(page_number=7, content="# Report\n## Evidence\nPage seven."),
            DocumentPage(page_number=8, content="Page eight."),
        )})
        await stage(sessions, doc, IndexProfile.PARENT_CHILD)
        original = await retrieval_result(sessions, IndexProfile.PARENT_CHILD)
        if corrupt:
            async with sessions() as session:
                async with session.begin():
                    await session.execute(update(KnowledgeContextRow).values(page_start=8, page_end=9))
            with pytest.raises(ContextResolutionIntegrityError, match="page span"):
                await resolve_knowledge_contexts(original, session_factory=sessions)
        else:
            result = await resolve_knowledge_contexts(original, session_factory=sessions)
            assert result.contexts[0].content == "Page seven.\nPage eight."
    asyncio.run(exercise())
