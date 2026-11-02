import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from risk_platform.knowledge.generation_evaluation import (
    GenerationAssessment, LLMGenerationEvaluator, measure_generation,
)
from .test_benchmark import question, results


ANSWER = "cause [1]; recovery [1]; invented [99]"


def assessment_data():
    return {
        "claims": [
            {"text": "cause", "supported_by_context": True, "correct": True, "supporting_citations": [1]},
            {"text": "recovery", "supported_by_context": True, "correct": True, "supporting_citations": [1]},
            {"text": "invented", "supported_by_context": False, "correct": False, "supporting_citations": []},
        ],
        "reference_facts": [{"fact_id": key, "covered_correctly": True} for key in ("cause", "recovery")],
        "citations": [{"number": 1, "supports_answer": True}, {"number": 99, "supports_answer": False}],
        "answer_relevancy": .9, "rationale": "Two correct supported facts; one invented claim and invalid citation.",
    }


def response(content, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
                           usage=SimpleNamespace(prompt_tokens=20, completion_tokens=10))


def client_with(*responses):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=list(responses)))))


def test_generation_metric_formulas_and_citation_denominators():
    _, resolved = results(parent=True)
    metrics = measure_generation(question(), ANSWER, resolved, GenerationAssessment.model_validate(assessment_data()))
    assert metrics["faithfulness"] == pytest.approx(2 / 3)
    assert metrics["hallucination_rate"] == pytest.approx(1 / 3)
    assert metrics["answer_fact_precision"] == pytest.approx(2 / 3)
    assert metrics["answer_fact_recall"] == 1
    assert metrics["answer_correctness"] == pytest.approx(.8)
    assert metrics["citation_precision"] == .5 and metrics["citation_recall"] == pytest.approx(2 / 3)
    assert metrics["invalid_citation_rate"] == .5


@pytest.mark.parametrize("fault", ["fact_missing", "fact_duplicate", "citation_missing", "invalid_citation",
                                  "unused_citation", "unsupported_claim", "unknown", "duplicate_claim", "empty_claim"])
def test_inconsistent_or_uncertain_judgments_fail_closed(fault):
    data = assessment_data()
    if fault == "fact_missing": data["reference_facts"].pop()
    if fault == "fact_duplicate": data["reference_facts"][1]["fact_id"] = "cause"
    if fault == "citation_missing": data["citations"].pop()
    if fault == "invalid_citation": data["citations"][1]["supports_answer"] = True
    if fault == "unused_citation": data["claims"][0]["supporting_citations"] = [2]
    if fault == "unsupported_claim": data["claims"][0]["supported_by_context"] = False
    if fault == "unknown": data["claims"][0]["correct"] = None
    if fault == "duplicate_claim": data["claims"][1]["text"] = "cause"
    if fault == "empty_claim": data["claims"][0]["text"] = " "
    with pytest.raises(ValueError):
        measure_generation(question(), ANSWER, results(parent=True)[1], GenerationAssessment.model_validate(data))


@pytest.mark.parametrize("field,value", [("answer_relevancy", 1.7), ("answer_relevancy", float("nan")),
                                        ("answer_relevancy", True), ("extra", "bad")])
def test_judge_schema_rejects_invalid_scores_and_extra_fields(field, value):
    with pytest.raises(ValidationError):
        GenerationAssessment.model_validate({**assessment_data(), field: value})


def test_no_claims_and_no_citations_are_not_perfect_faithfulness():
    data = {"claims": [], "citations": [], "answer_relevancy": 0.0, "rationale": "abstained",
            "reference_facts": [{"fact_id": key, "covered_correctly": False} for key in ("cause", "recovery")]}
    metrics = measure_generation(question(), "insufficient information", results(())[1], GenerationAssessment.model_validate(data))
    assert metrics["faithfulness"] is None and metrics["citation_precision"] is None
    assert metrics["answer_correctness"] == 0


def test_zero_citation_is_counted_as_invalid_instead_of_dropped():
    data = assessment_data()
    data["citations"][1]["number"] = 0
    metrics = measure_generation(question(), ANSWER.replace("[99]", "[0]"), results(parent=True)[1],
                                 GenerationAssessment.model_validate(data))
    assert metrics["invalid_citation_rate"] == .5


def test_llm_adapter_calls_both_models_and_hides_reference_from_answer_model():
    client = client_with(response(ANSWER), response(json.dumps(assessment_data())))
    evaluator = LLMGenerationEvaluator(client=client, answer_model="answer-model", judge_model="judge-model")
    q = question().model_copy(update={"reference_answer": "cause and recovery"})
    resolved = results(parent=True)[1]
    report = asyncio.run(evaluator.evaluate(q, resolved))
    assert report["status"] == "SUCCEEDED" and report["metrics"]["answer_correctness"] == pytest.approx(.8)
    assert report["snapshot"] == resolved.snapshot.model_dump(mode="json")
    assert report["prompt_tokens"] == 20 and report["judge_completion_tokens"] == 10
    calls = client.chat.completions.create.call_args_list
    assert calls[0].kwargs["model"] == "answer-model" and calls[1].kwargs["model"] == "judge-model"
    first = json.loads(calls[0].kwargs["messages"][1]["content"])
    assert "reference_answer" not in first and "reference_facts" not in first
    assert first["contexts"][0]["content"] == resolved.contexts[0].content
    assert "supporting_hits" not in first["contexts"][0]
    assert "content" not in first["contexts"][0]["sources"][0]
    assert json.loads(calls[1].kwargs["messages"][1]["content"])["reference_answer"] == q.reference_answer
    assert report["same_model_judge"] is False and len(report["context_digest"]) == 64


@pytest.mark.parametrize("fault", ["provider", "json", "truncated", "empty", "missing_reference", "uncertain"])
def test_llm_failures_are_inconclusive_without_scores_or_secret_exception_text(fault):
    judge = assessment_data()
    if fault == "uncertain": judge["claims"][0]["correct"] = None
    generated = response("" if fault == "empty" else ANSWER)
    judged = response("not json" if fault == "json" else json.dumps(judge), "length" if fault == "truncated" else "stop")
    client = client_with(RuntimeError("secret-provider-body") if fault == "provider" else generated, judged)
    evaluator = LLMGenerationEvaluator(client=client, answer_model="answer", judge_model="judge")
    q = question() if fault == "missing_reference" else question().model_copy(update={"reference_answer": "reference"})
    report = asyncio.run(evaluator.evaluate(q, results(parent=True)[1]))
    assert report["status"] == "INCONCLUSIVE" and "metrics" not in report
    assert "secret-provider-body" not in json.dumps(report)
    if fault == "missing_reference": client.chat.completions.create.assert_not_called()
    if fault in ("json", "truncated", "uncertain"):
        assert report["judge_completion_tokens"] == 10


@pytest.mark.parametrize("incomplete", [False, True])
def test_cli_generation_summary_preserves_failures_and_closes_client(tmp_path, monkeypatch, incomplete):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).parents[3]
    spec = importlib.util.spec_from_file_location("generation_cli", root / "scripts/benchmark_knowledge.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr("openai.AsyncOpenAI", lambda **_: client)
    monkeypatch.setenv("RAG_EVAL_API_KEY", "test-eval-key")
    monkeypatch.setattr(cli, "get_embedding_client", lambda: object())
    generations = [
        {"status": "SUCCEEDED", "metrics": {"faithfulness": .5}, "generation_ms": 10, "judge_ms": 5,
         "prompt_tokens": 20, "completion_tokens": 10, "judge_prompt_tokens": 30, "judge_completion_tokens": 15},
        {"status": "SUCCEEDED", "metrics": {"faithfulness": 1.0}, "generation_ms": 12, "judge_ms": 6,
         "prompt_tokens": 25, "completion_tokens": 11, "judge_prompt_tokens": 31, "judge_completion_tokens": 16},
    ]
    if incomplete: generations[0] = {"status": "INCONCLUSIVE", "error_type": "RuntimeError"}
    rows = [{"profile": profile, "metrics": {"retrieval_recall_at_k": .5}, "total_ms": 3,
             "generation": generation} for profile, generation in zip(("baseline-v1", "parent-child-v1"), generations)]
    runner = AsyncMock(return_value={"status": "INCONCLUSIVE" if incomplete else "SUCCEEDED", "rows": rows})
    monkeypatch.setattr(cli, "run_benchmark", runner)
    args = SimpleNamespace(dataset=root / "backend/tests/fixtures/knowledge_benchmark_v2.json", output=tmp_path / "report.json",
                           repeats=1, tokenizer_json=None, evaluate_generation=True, answer_model="answer", judge_model="judge")
    assert asyncio.run(cli.execute(args)) == (2 if incomplete else 0)
    report = json.loads(args.output.read_text(encoding="utf-8"))
    baseline = report["summary"]["baseline-v1"]
    assert baseline["generation_macro_metrics"] == ({} if incomplete else {"faithfulness": .5})
    assert baseline["prompt_tokens_total"] == (None if incomplete else 20)
    assert runner.call_args.kwargs["generation_evaluator"].judge_model == "judge"
    client.close.assert_awaited_once()
