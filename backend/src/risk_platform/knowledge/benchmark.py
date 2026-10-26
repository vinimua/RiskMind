"""Offline, exact-quote evaluation of the sealed dense/context pipelines.

Gold quotes measure relevant context availability, never executable Evidence or
answer correctness. This module neither stages knowledge nor changes routes.
"""

import hashlib
import json
from time import perf_counter
from typing import Callable, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from risk_platform.knowledge.chunking import KnowledgeDocument
from risk_platform.knowledge.context_resolution import resolve_knowledge_contexts
from risk_platform.knowledge.contracts import KnowledgeSearchRequest
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import retrieve_dense_children


async def verify_benchmark_corpus(dataset, sessions):
    """Require both accepted serving snapshots to contain exactly this corpus.

    Dedicated benchmark database/collections are expected. Never silently compare
    a partial/older serving build against a newer gold corpus.
    """
    from sqlalchemy import select
    from risk_platform.knowledge import milvus
    from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
    from risk_platform.knowledge.models import KnowledgeChunkRow, KnowledgeCollectionBuildRow, KnowledgeDocumentRow
    from risk_platform.knowledge.serving import authoritative_snapshot, resolve_serving_route

    snapshots = {}
    async with sessions() as session:
        for doc in dataset.documents:
            row = await session.get(KnowledgeDocumentRow, (doc.document_id, doc.document_version))
            if row is None or KnowledgeDocument.model_validate_json(row.source_json) != doc:
                raise ValueError("benchmark canonical source differs from dataset")
        for profile in IndexProfile:
            strategy = BaselineIndexBuildStrategy() if profile == IndexProfile.BASELINE else ParentChildIndexBuildStrategy()
            expected = {chunk.chunk_id: chunk for doc in dataset.documents for chunk in strategy.build(doc).search_chunks}
            rows = list((await session.execute(select(KnowledgeChunkRow).where(
                KnowledgeChunkRow.index_profile == profile.value,
            ))).scalars())
            if len(rows) != len(expected) or any(
                row.chunk_id not in expected or any(getattr(row, key) != getattr(expected[row.chunk_id], key)
                                                  for key in milvus.OUTPUT_FIELDS) for row in rows
            ):
                raise ValueError("benchmark profile children differ from the fixed corpus")
            snapshot = await resolve_serving_route(session, profile)
            snapshots[profile.value] = snapshot
            build = (await session.execute(select(KnowledgeCollectionBuildRow).where(
                KnowledgeCollectionBuildRow.collection == snapshot.collection,
                KnowledgeCollectionBuildRow.index_profile == profile.value,
            ))).scalar_one()
            if await authoritative_snapshot(session, profile) != (build.source_chunk_count, build.authoritative_snapshot_digest):
                raise ValueError("benchmark serving acceptance does not cover the current corpus")
    return snapshots


class BenchmarkContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldQuote(BenchmarkContract):
    quote_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    text: str = Field(min_length=1)


class BenchmarkQuestion(BenchmarkContract):
    question_id: str = Field(min_length=1)
    request: KnowledgeSearchRequest
    relevant_quotes: tuple[GoldQuote, ...] = Field(min_length=1)


class BenchmarkDataset(BenchmarkContract):
    dataset_id: str = Field(min_length=1)
    annotation_notes: str = Field(min_length=1)
    documents: tuple[KnowledgeDocument, ...] = Field(min_length=1)
    questions: tuple[BenchmarkQuestion, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_gold(self) -> Self:
        documents = {(doc.document_id, doc.document_version): doc for doc in self.documents}
        if len(documents) != len(self.documents):
            raise ValueError("duplicate corpus source")
        if len({q.question_id for q in self.questions}) != len(self.questions):
            raise ValueError("duplicate question ID")
        for question in self.questions:
            quotes = question.relevant_quotes
            if len({q.quote_id for q in quotes}) != len(quotes):
                raise ValueError("duplicate gold quote ID")
            identities = {(q.document_id, q.document_version, q.text) for q in quotes}
            if len(identities) != len(quotes):
                raise ValueError("duplicate gold quote")
            for quote in quotes:
                doc = documents.get((quote.document_id, quote.document_version))
                if doc is None or not quote.text.strip() or not any(quote.text in p.content for p in doc.pages):
                    raise ValueError("gold quote must occur verbatim in its corpus source")
        return self


def dataset_digest(dataset: BenchmarkDataset) -> str:
    payload = json.dumps(dataset.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def measure_contexts(question, retrieved, resolved, *, count_tokens: Callable[[str], int] | None = None):
    """Whole-quote matching is deliberately conservative about partial coverage.

    Context precision counts contexts containing a gold quote. Coverage weights
    *fully* covered quotes by character length; it is not token precision.
    """
    if retrieved.snapshot != resolved.snapshot:
        raise ValueError("context snapshot differs from retrieval")
    expected = {item.hit.source.chunk_id: item.hit for item in retrieved.hits}
    support = [hit for context in resolved.contexts for hit in context.supporting_hits]
    if len(support) != len(expected) or {hit.source.chunk_id for hit in support} != set(expected):
        raise ValueError("contexts must partition retrieved children")
    if any(hit != expected[hit.source.chunk_id] for hit in support):
        raise ValueError("supporting child payload differs from retrieval")

    def matches(text, hits):
        sources = {(hit.source.document_id, hit.source.document_version) for hit in hits}
        return {q.quote_id for q in question.relevant_quotes
                if (q.document_id, q.document_version) in sources and q.text in text}

    child_sets = [matches(item.hit.content, (item.hit,)) for item in retrieved.hits]
    context_sets = [matches(context.content, context.supporting_hits) for context in resolved.contexts]
    children = set().union(*child_sets)
    contexts = set().union(*context_sets)
    quote_matches = sum(len(ids) for ids in context_sets)
    child_chars = sum(len(item.hit.content) for item in retrieved.hits)
    context_chars = sum(len(context.content) for context in resolved.contexts)
    counts = None
    if count_tokens is not None:
        counts = [count_tokens(context.content) for context in resolved.contexts]
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("token counter must return nonnegative integers")
    return {
        "child_relevant_quote_recall": len(children) / len(question.relevant_quotes),
        "context_relevant_quote_recall": len(contexts) / len(question.relevant_quotes),
        "context_precision": sum(bool(ids) for ids in context_sets) / len(context_sets) if context_sets else 0.0,
        "context_coverage": sum(len(q.text) for q in question.relevant_quotes if q.quote_id in contexts)
                            / sum(len(q.text) for q in question.relevant_quotes),
        "complete_relevant_context": len(contexts) == len(question.relevant_quotes),
        "context_tokens": sum(counts) if counts is not None else None,
        "context_characters": context_chars,
        "context_to_child_character_ratio": context_chars / child_chars if child_chars else None,
        "redundant_gold_match_ratio": (quote_matches - len(contexts)) / quote_matches if quote_matches else 0.0,
        "child_count": len(retrieved.hits), "context_count": len(resolved.contexts),
        "covered_quote_ids": sorted(contexts),
        "missing_quote_ids": sorted(q.quote_id for q in question.relevant_quotes if q.quote_id not in contexts),
    }


async def run_benchmark(dataset: BenchmarkDataset, *, repeats=3, count_tokens=None, **pipeline_options):
    """Read-only paired runs, alternating profile order, strict route stability.

    Latency includes embedding, Milvus and authoritative PG access, excluding
    scoring/tokenization. Dependency/integrity errors invalidate the entire run.
    Raw hits/contexts and route snapshots are retained for manual audit.
    """
    dataset = BenchmarkDataset.model_validate_json(dataset.model_dump_json())
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be positive")
    from risk_platform.db.database import get_session_factory
    sessions = pipeline_options.get("session_factory") or get_session_factory()
    snapshots = await verify_benchmark_corpus(dataset, sessions)
    rows = []
    for repeat in range(repeats):
        for index, question in enumerate(dataset.questions):
            profiles = list(IndexProfile)
            if (repeat + index) % 2:
                profiles.reverse()
            for profile in profiles:
                start = perf_counter()
                retrieved = await retrieve_dense_children(question.request, index_profile=profile, **pipeline_options)
                after_search = perf_counter()
                resolved = await resolve_knowledge_contexts(retrieved, session_factory=pipeline_options.get("session_factory"))
                end = perf_counter()
                previous = snapshots.setdefault(profile.value, retrieved.snapshot)
                if previous != retrieved.snapshot:
                    raise ValueError("serving route changed during benchmark; rerun on stable routes")
                rows.append({
                    "question_id": question.question_id, "repeat": repeat, "profile": profile.value,
                    "retrieval_ms": (after_search - start) * 1000,
                    "resolution_ms": (end - after_search) * 1000,
                    "total_ms": (end - start) * 1000,
                    "metrics": measure_contexts(question, retrieved, resolved, count_tokens=count_tokens),
                    "retrieved": retrieved.model_dump(mode="json"), "resolved": resolved.model_dump(mode="json"),
                })
    if snapshots != await verify_benchmark_corpus(dataset, sessions):
        raise ValueError("serving route changed during benchmark; rerun on stable routes")
    return {"status": "SUCCEEDED", "dataset_id": dataset.dataset_id,
            "dataset_digest": dataset_digest(dataset), "repeats": repeats, "rows": rows}
