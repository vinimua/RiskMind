import math
from pathlib import Path

import pytest

from risk_platform.knowledge.benchmark import BenchmarkDataset, dataset_digest
from risk_platform.knowledge.indexing import BaselineIndexBuildStrategy, ParentChildIndexBuildStrategy
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval_metrics import measure_retrieval, relevant_child_ids
from .test_benchmark import question, results
from .test_profile_persistence import document


def test_rank_metrics_use_complete_gold_set_and_requested_k():
    retrieved, _ = results()
    metrics = measure_retrieval(retrieved, relevant_ids=frozenset({"0", "unretrieved"}), top_k=3)
    assert metrics["retrieval_precision_at_k"] == 1 / 3
    assert metrics["retrieval_recall_at_k"] == .5
    assert metrics["retrieval_mrr_at_k"] == 1
    assert metrics["retrieval_ndcg_at_k"] == pytest.approx(1 / (1 + 1 / math.log2(3)))
    assert metrics["retrieval_hit_at_k"] is True


def test_rank_changes_mrr_and_ndcg():
    retrieved, _ = results()
    metrics = measure_retrieval(retrieved, relevant_ids=frozenset({"1"}), top_k=2)
    assert metrics["retrieval_mrr_at_k"] == .5
    assert metrics["retrieval_ndcg_at_k"] == pytest.approx(1 / math.log2(3))


def test_empty_results_and_unreachable_gold_have_explicit_denominators():
    retrieved, _ = results(())
    metrics = measure_retrieval(retrieved, relevant_ids=frozenset({"x"}), top_k=5)
    assert metrics["retrieval_recall_at_k"] == metrics["retrieval_mrr_at_k"] == 0
    metrics = measure_retrieval(retrieved, relevant_ids=frozenset(), top_k=5)
    assert metrics["retrieval_recall_at_k"] is None and metrics["retrieval_ndcg_at_k"] is None
    assert metrics["retrieval_precision_at_k"] == 0


@pytest.mark.parametrize("fault", ["duplicate", "rank", "top_k", "bool"])
def test_invalid_rank_inputs_fail(fault):
    retrieved, _ = results()
    if fault == "duplicate":
        retrieved = retrieved.model_copy(update={"hits": (retrieved.hits[0], retrieved.hits[0])})
    if fault == "rank":
        retrieved = retrieved.model_copy(update={"hits": tuple(reversed(retrieved.hits))})
    with pytest.raises(ValueError):
        measure_retrieval(retrieved, relevant_ids=frozenset(), top_k=True if fault == "bool" else 1 if fault == "top_k" else 2)


@pytest.mark.parametrize("profile,strategy", [(IndexProfile.BASELINE, BaselineIndexBuildStrategy()),
                                             (IndexProfile.PARENT_CHILD, ParentChildIndexBuildStrategy())])
def test_labels_are_source_and_profile_specific(profile, strategy):
    doc = document("cause recovery")
    ids = relevant_child_ids(question(), (doc,), profile)
    assert ids == {chunk.chunk_id for chunk in strategy.build(doc).search_chunks}
    assert not relevant_child_ids(question(), (doc.model_copy(update={"document_id": "wrong"}),), profile)


def test_old_dataset_digest_is_preserved_and_v2_has_reference_answers():
    fixtures = Path(__file__).parents[1] / "fixtures"
    old = BenchmarkDataset.model_validate_json((fixtures / "knowledge_benchmark_v1.json").read_text(encoding="utf-8"))
    assert dataset_digest(old) == "c1af0bed035b8b335ff85ab1c72eb72c79dd1dfd93181eecabc42bdda7ad7931"
    new = BenchmarkDataset.model_validate_json((fixtures / "knowledge_benchmark_v2.json").read_text(encoding="utf-8"))
    assert all(question.reference_answer for question in new.questions)
    assert new.documents == old.documents and dataset_digest(new) != dataset_digest(old)
