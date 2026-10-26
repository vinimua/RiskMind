import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from risk_platform.knowledge import benchmark
from risk_platform.knowledge.benchmark import BenchmarkDataset, BenchmarkQuestion, GoldQuote, measure_contexts
from risk_platform.knowledge.context_resolution import ContextResolutionResult
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSearchRequest, KnowledgeSourceRef
from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult, RetrievedKnowledgeHit
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot
from .test_profile_persistence import ProfileMilvus, document, sessions
from .test_retrieval import QueryEmbedding, remote_rows, configure_search
from .test_serving import candidate, activate, inline_milvus_and_sqlite_timezones


def question():
    return BenchmarkQuestion(question_id="q", request=KnowledgeSearchRequest(query="why", top_k=5), relevant_quotes=(
        GoldQuote(quote_id="cause", document_id="REPORT-1", document_version="v1", text="cause"),
        GoldQuote(quote_id="recovery", document_id="REPORT-1", document_version="v1", text="recovery"),
    ))


def results(texts=("cause", "noise"), parent=False, wrong_source=False):
    snapshot = RetrievalRouteSnapshot(index_profile=IndexProfile.PARENT_CHILD if parent else IndexProfile.BASELINE,
                                      collection="physical", route_version=1)
    hits = tuple(KnowledgeHit(source=KnowledgeSourceRef(document_id="wrong" if wrong_source else "REPORT-1",
                                                       document_version="v1", chunk_id=str(i)),
                              document_type="MODEL_DEVELOPMENT_REPORT", title="Report", section_path="Analysis",
                              content=text, rank=i + 1) for i, text in enumerate(texts))
    retrieved = DenseChildRetrievalResult(snapshot=snapshot, hits=tuple(
        RetrievedKnowledgeHit(hit=hit, score=.9 - i * .1) for i, hit in enumerate(hits)))
    contexts = ((KnowledgeContext(context_id="parent", content="cause and recovery", supporting_hits=hits),)
                if parent else tuple(KnowledgeContext(context_id=hit.source.chunk_id, content=hit.content,
                                                      supporting_hits=(hit,)) for hit in hits))
    return retrieved, ContextResolutionResult(snapshot=snapshot, contexts=contexts)


def test_metrics_separate_child_recall_parent_expansion_and_complete_context():
    direct = measure_contexts(question(), *results(), count_tokens=lambda text: len(text.split()))
    expanded = measure_contexts(question(), *results(parent=True))
    assert direct["child_relevant_quote_recall"] == expanded["child_relevant_quote_recall"] == .5
    assert direct["context_relevant_quote_recall"] == .5
    assert expanded["context_relevant_quote_recall"] == 1
    assert direct["context_precision"] == .5 and expanded["context_precision"] == 1
    assert direct["context_coverage"] == 5 / 13 and expanded["context_coverage"] == 1
    assert direct["complete_relevant_context"] is False and expanded["complete_relevant_context"] is True
    assert direct["context_tokens"] == 2 and expanded["context_tokens"] is None
    assert expanded["context_to_child_character_ratio"] == 18 / 10
    assert direct["missing_quote_ids"] == ["recovery"]


def test_repeated_gold_is_reported_as_redundancy_not_extra_recall():
    metrics = measure_contexts(question(), *results(("cause", "cause")))
    assert metrics["redundant_gold_match_ratio"] == .5
    assert metrics["context_relevant_quote_recall"] == .5


def test_wrong_source_partial_quote_and_empty_are_not_successful_coverage():
    for pair in (results(wrong_source=True), results(("caus",)), results(())):
        metrics = measure_contexts(question(), *pair)
        assert metrics["context_relevant_quote_recall"] == 0
        assert not metrics["complete_relevant_context"]
    assert measure_contexts(question(), *results(()))["context_to_child_character_ratio"] is None


def test_metrics_reject_snapshot_and_support_corruption():
    retrieved, resolved = results()
    with pytest.raises(ValueError, match="snapshot"):
        measure_contexts(question(), retrieved, resolved.model_copy(update={
            "snapshot": resolved.snapshot.model_copy(update={"route_version": 2})}))
    with pytest.raises(ValueError, match="partition"):
        measure_contexts(question(), retrieved, resolved.model_copy(update={"contexts": ()}))
    with pytest.raises(ValueError, match="nonnegative"):
        measure_contexts(question(), retrieved, resolved, count_tokens=lambda _: -1)


def test_versioned_gold_is_valid_and_buildable_without_algorithm_changes():
    path = Path(__file__).parents[1] / "fixtures/knowledge_benchmark_v1.json"
    dataset = BenchmarkDataset.model_validate_json(path.read_text(encoding="utf-8"))
    assert len(dataset.questions) == 6 and len(dataset.documents) == 2
    assert benchmark.dataset_digest(dataset) == benchmark.dataset_digest(dataset.model_copy(deep=True))
    for strategy in (BaselineIndexBuildStrategy(), ParentChildIndexBuildStrategy()):
        assert all(strategy.build(doc).search_chunks for doc in dataset.documents)
    invalid = dataset.model_dump()
    invalid["questions"][0]["relevant_quotes"][0]["text"] = "nonexistent gold"
    with pytest.raises(ValidationError, match="verbatim"):
        BenchmarkDataset.model_validate(invalid)
    with pytest.raises(ValidationError):
        BenchmarkDataset.model_validate({**dataset.model_dump(), "unexpected": True})


def test_paired_runner_calls_sealed_pipeline_and_rejects_source_changes(sessions):
    async def exercise():
        client = ProfileMilvus()
        for profile in IndexProfile:
            receipt = await candidate(sessions, client, profile.value, profile)
            await activate(sessions, client, receipt, key=profile.value)
        dataset = BenchmarkDataset(dataset_id="smoke", annotation_notes="fixed vector plumbing only",
            documents=(document(),), questions=(BenchmarkQuestion(question_id="q",
                request=KnowledgeSearchRequest(query="Text", top_k=1), relevant_quotes=(
                    GoldQuote(quote_id="text", document_id="REPORT-1", document_version="v1", text="Text."),)),))
        original_search = client.search
        def search(*args, **kwargs):
            collection = kwargs.get("collection_name") or args[0]
            configure_search(client, remote_rows(client, collection))
            return original_search(*args, **kwargs)
        client.search = search
        report = await benchmark.run_benchmark(dataset, repeats=2, session_factory=sessions,
                                               milvus_client=client, embedding_client=QueryEmbedding())
        assert report["status"] == "SUCCEEDED"
        assert [row["profile"] for row in report["rows"]] == [
            "baseline-v1", "parent-child-v1", "parent-child-v1", "baseline-v1"]
        assert all(row["metrics"]["context_relevant_quote_recall"] == 1 for row in report["rows"])
        invalid = dataset.model_copy(update={"documents": (document("Text. changed"),)})
        with pytest.raises(ValueError, match="canonical source"):
            await benchmark.run_benchmark(invalid, session_factory=sessions)
    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["route", "dependency"])
def test_runner_fails_whole_run_on_route_or_dependency_failure(monkeypatch, fault):
    retrieved, resolved = results()
    dataset = BenchmarkDataset(dataset_id="unit", annotation_notes="unit", documents=(document("cause recovery"),),
                               questions=(question(),))
    async def verify(*_):
        return {"baseline-v1": retrieved.snapshot}
    async def retrieve(*_, **options):
        if fault == "dependency":
            raise RuntimeError("unavailable")
        return retrieved.model_copy(update={"snapshot": retrieved.snapshot.model_copy(update={"route_version": 2})})
    async def resolve(*_, **__):
        return resolved
    monkeypatch.setattr(benchmark, "verify_benchmark_corpus", verify)
    monkeypatch.setattr(benchmark, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(benchmark, "resolve_knowledge_contexts", resolve)
    with pytest.raises((ValueError, RuntimeError), match="changed|unavailable"):
        asyncio.run(benchmark.run_benchmark(dataset, session_factory=object()))


@pytest.mark.parametrize("fault", ["configuration", "summary"])
def test_cli_failure_writes_inconclusive_without_dependency_secrets(tmp_path, monkeypatch, fault):
    import importlib.util
    import json
    from types import SimpleNamespace

    root = Path(__file__).parents[3]
    spec = importlib.util.spec_from_file_location("benchmark_cli", root / "scripts/benchmark_knowledge.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    def unavailable():
        raise RuntimeError("sensitive-provider-body")
    if fault == "configuration":
        monkeypatch.setattr(cli, "get_embedding_client", unavailable)
    else:
        monkeypatch.setattr(cli, "get_embedding_client", lambda: object())
        async def malformed_result(*_, **__):
            return {"status": "SUCCEEDED", "rows": []}
        monkeypatch.setattr(cli, "run_benchmark", malformed_result)
    output = tmp_path / "result.json"
    args = SimpleNamespace(dataset=root / "backend/tests/fixtures/knowledge_benchmark_v1.json",
                           output=output, repeats=1, tokenizer_json=None)
    assert asyncio.run(cli.execute(args)) == 2
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "INCONCLUSIVE"
    assert result["stage"] == ("embedding_configuration" if fault == "configuration" else "pipeline")
    assert "rows" not in result and "sensitive-provider-body" not in output.read_text()
