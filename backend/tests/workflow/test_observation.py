"""7C 的执行事实、来源链与解释失败语义；不使用真实模型。"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from risk_platform.execution.contracts import (
    ExecutionError, KnowledgeExecutionContext, KnowledgeExecutionPayload, KnowledgeSearchToolRequest, TaskExecutionResult,
)
from risk_platform.knowledge.context_resolution import ContextResolutionResult
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSourceRef
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult, RetrievedKnowledgeHit
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot
from risk_platform.workflow import nodes
from risk_platform.workflow.contracts import CapabilityType, InitialPlanningInput, InvestigationPlan, InvestigationTask, TaskStatus, UserQuery
from risk_platform.workflow.graph import build_investigation_graph
from test_runtime import make_runtime
from risk_platform.workflow.observation import (
    KnowledgeObservationDraft, ObservationIntegrityError, validate_round_inputs,
)


def execution(index=0, status="SUCCEEDED", empty=False):
    now = datetime.now(timezone.utc)
    payload = None
    error = None
    if status == "SUCCEEDED":
        snapshot = RetrievalRouteSnapshot(index_profile=IndexProfile.PARENT_CHILD, collection="pc", route_version=1)
        hit = KnowledgeHit(source=KnowledgeSourceRef(document_id=f"D{index}", document_version="v1", chunk_id=f"C{index}"),
                           document_type="INCIDENT_REPORT", title="历史报告", section_path="根因", content="标签未成熟", rank=1)
        retrieved = DenseChildRetrievalResult(snapshot=snapshot, hits=() if empty else (RetrievedKnowledgeHit(hit=hit, score=.8),))
        resolved = ContextResolutionResult(snapshot=snapshot, contexts=() if empty else (
            KnowledgeContext(context_id=f"P{index}", content="历史案例中标签未成熟导致指标低估。", supporting_hits=(hit,)),))
        payload = KnowledgeExecutionPayload(request=KnowledgeSearchToolRequest(query="历史案例", top_k=1),
                                            retrieved=retrieved, resolved=resolved)
    else:
        error = ExecutionError(category="DEPENDENCY" if status=="FAILED" else "AUTHORIZATION",
                               code="DEPENDENCY_FAILURE" if status=="FAILED" else "TOOL_NOT_AUTHORIZED",
                               message="执行未能完成", retryable=False)
    return TaskExecutionResult(execution_id=f"E{index}", task_id=f"T{index}", capability="KNOWLEDGE_RETRIEVAL",
                               status=status, started_at=now, completed_at=now, payload=payload, error=error)


def current(results):
    return {"planning_input": InitialPlanningInput(user_query=UserQuery(query="为什么下降？"),
                                                   available_capabilities=[CapabilityType.KNOWLEDGE_RETRIEVAL]),
            "investigation_plan": InvestigationPlan(goal="调查背景", tasks=[InvestigationTask(
                task_id=r.task_id, objective="查历史", expected_result="知识线索", capability=r.capability,
                status="COMPLETED" if r.status=="SUCCEEDED" else "FAILED") for r in results]),
            "task_execution_results": results}


def draft(results):
    available = next(r for r in results if r.payload and r.payload.resolved.contexts)
    context = available.payload.resolved.contexts[0]
    return {"execution_refs": [r.execution_id for r in results],
            "findings": [{"statement": "历史案例中标签未成熟曾低估指标", "execution_refs": [available.execution_id],
                          "sources": [{"execution_id": available.execution_id, "context_id": context.context_id,
                                       "source": context.supporting_hits[0].source.model_dump(mode="json"),
                                       "quote": "标签未成熟导致指标低估"}],
                          "limitation": "这是历史案例，尚未验证当前案件的标签成熟度"}],
            "knowledge_gaps": [{"text": "尚缺当前标签成熟度", "execution_refs": [available.execution_id]}],
            "next_step_suggestions": [{"text": "建议后续获取当前标签成熟度", "execution_refs": [available.execution_id]}],
            "narrative": "历史经验提示调查方向，但当前案件原因仍未确认。"}


def run(results, data=None, failure=None):
    class Agent:
        def observe_knowledge_round(self, *_):
            if failure:
                raise failure
            return KnowledgeObservationDraft.model_validate(data if data is not None else draft(results))
    return asyncio.run(nodes.observation(current(results), investigation_agent=Agent(), execution_context=KnowledgeExecutionContext(
        actor_id="trusted", index_profile=IndexProfile.PARENT_CHILD)))['observation']


def test_mixed_results_keep_failure_facts_and_traceable_historical_finding():
    results = [execution(), execution(1, "FAILED"), execution(2, "REJECTED"), execution(3, empty=True)]
    observed = run(results)
    assert observed.status == "SUCCEEDED" and observed.error is None
    assert observed.knowledge_scope == "HISTORICAL_KNOWLEDGE_ONLY"
    assert [s.outcome for s in observed.execution_summaries] == ["KNOWLEDGE_AVAILABLE", "EXECUTION_FAILED", "EXECUTION_REJECTED", "NO_HITS"]
    assert len(observed.knowledge_gaps) == 4  # 三项系统缺口 + 一项模型建议的信息缺口。
    source = observed.findings[0].sources[0]
    assert source.execution_id == "E0" and source.context_id == "P0" and source.source.chunk_id == "C0"
    assert observed.observation_id.startswith("obs-") and observed.created_at.tzinfo


@pytest.mark.parametrize("status,empty,outcome", [("SUCCEEDED", True, "NO_HITS"),
                                                  ("FAILED", False, "EXECUTION_FAILED"),
                                                  ("REJECTED", False, "EXECUTION_REJECTED")])
def test_no_available_knowledge_never_calls_explanation_model(status, empty, outcome):
    result = execution(status=status, empty=empty)
    observed = run([result], failure=AssertionError("不应调用模型"))
    assert observed.status == "INCONCLUSIVE" and observed.error.code == "NO_KNOWLEDGE_AVAILABLE"
    assert not observed.findings and not observed.next_step_suggestions
    assert observed.execution_summaries[0].outcome == outcome


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "incomplete", "wrong-context", "wrong-source", "wrong-quote", "failure-source"])
def test_invalid_draft_rejects_entire_interpretation(fault):
    results = [execution(), execution(1, "FAILED")]
    data = draft(results)
    finding = data["findings"][0]
    if fault == "unknown":
        data["knowledge_gaps"][0]["execution_refs"] = ["old-round-execution"]
    elif fault == "duplicate":
        data["execution_refs"] = ["E0", "E0", "E1"]
    elif fault == "incomplete":
        data["execution_refs"] = ["E0"]
    elif fault == "wrong-context":
        finding["sources"][0]["context_id"] = "OTHER"
    elif fault == "wrong-source":
        finding["sources"][0]["source"]["document_version"] = "v999"
    elif fault == "wrong-quote":
        finding["sources"][0]["quote"] = "不存在的引文"
    else:
        finding["execution_refs"] = ["E1"]
        finding["sources"][0]["execution_id"] = "E1"
    observed = run(results, data)
    assert observed.status == "INCONCLUSIVE" and observed.error.code == "OBSERVATION_INVALID"
    assert not observed.findings and not observed.next_step_suggestions


@pytest.mark.parametrize("field", ["observation_id", "status", "created_at", "root_cause", "hypotheses", "evidence"])
def test_model_cannot_add_system_identity_or_root_cause_fields(field):
    results = [execution()]
    data = {**draft(results), field: "forged"}
    assert run(results, data).error.code == "OBSERVATION_INVALID"


@pytest.mark.parametrize("failure,code", [(TimeoutError("secret"), "OBSERVATION_TIMEOUT"),
                                         (ValueError("secret"), "OBSERVATION_INVALID"),
                                         (RuntimeError("secret"), "OBSERVATION_FAILED")])
def test_model_failure_has_no_partial_draft_and_no_secret(failure, code):
    observed = run([execution()], failure=failure)
    assert observed.status == "INCONCLUSIVE" and observed.error.code == code
    assert not observed.findings and "secret" not in observed.model_dump_json()


@pytest.mark.parametrize("fault", ["missing", "duplicate-task", "duplicate-execution", "wrong-task", "status"])
def test_round_input_corruption_rejected_before_model(fault):
    results = [execution(), execution(1)]
    state = current(results)
    if fault == "missing":
        results.pop()
    elif fault == "duplicate-task":
        state["investigation_plan"].tasks[1].task_id = "T0"
    elif fault == "duplicate-execution":
        results[1] = results[1].model_copy(update={"execution_id": "E0"})
    elif fault == "wrong-task":
        results[1] = results[1].model_copy(update={"task_id": "OTHER"})
    else:
        state["investigation_plan"].tasks[0].status = TaskStatus.FAILED
    with pytest.raises(ObservationIntegrityError):
        validate_round_inputs(state["investigation_plan"], results)


def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        run([execution()], failure=asyncio.CancelledError())


def test_actual_deadline_without_partial_output():
    import time
    results = [execution()]
    class Slow:
        def observe_knowledge_round(self, *_):
            time.sleep(.04)
            return KnowledgeObservationDraft.model_validate(draft(results))
    ctx = KnowledgeExecutionContext(actor_id="a", index_profile=IndexProfile.PARENT_CHILD, timeout_seconds=.001)
    result = asyncio.run(nodes.observation(current(results), investigation_agent=Slow(), execution_context=ctx))
    assert result["observation"].error.code == "OBSERVATION_TIMEOUT"


@pytest.mark.parametrize("bad_model", [False, True])
def test_graph_observation_does_not_close_case_or_change_execution_results(monkeypatch, bad_model):
    results = [execution()]
    state = current(results)
    pending = state["investigation_plan"].model_copy(deep=True)
    pending.tasks[0].status = TaskStatus.PENDING
    async def execute(*_, **__):
        return results[0]
    monkeypatch.setattr(nodes, "execute_knowledge_search", execute)
    class Agent:
        def initial_plan(self, _):
            return pending
        def formulate_knowledge_query(self, *_, **__):
            return {"query": "具体问题", "top_k": 1}
        def observe_knowledge_round(self, *_):
            if bad_model:
                raise ValueError("private")
            return KnowledgeObservationDraft.model_validate(draft(results))
    graph = build_investigation_graph(Agent())
    output = asyncio.run(graph.ainvoke({"planning_input": state["planning_input"]}, context=make_runtime(
        state["planning_input"], KnowledgeExecutionContext(actor_id="a", index_profile=IndexProfile.PARENT_CHILD),
        allow_unscoped=True)))
    assert output["round_complete"] is True and output["investigation_status"] == "OPEN"
    assert output["task_execution_results"] == results
    assert output["observation"].status == ("INCONCLUSIVE" if bad_model else "SUCCEEDED")
    assert "hypotheses" not in output and "investigation_complete" not in output


def test_source_quote_must_belong_to_same_execution_even_if_context_id_repeats():
    results = [execution(), execution(1)]
    data = draft(results)
    data["findings"][0]["sources"][0]["source"] = results[1].payload.retrieved.hits[0].hit.source.model_dump(mode="json")
    assert run(results, data).error.code == "OBSERVATION_INVALID"


def test_invalid_second_finding_does_not_keep_valid_first_finding():
    import copy
    results = [execution()]
    data = draft(results)
    bad = copy.deepcopy(data["findings"][0])
    bad["sources"][0]["quote"] = "模型虚构的引文"
    data["findings"].append(bad)
    observed = run(results, data)
    assert observed.status == "INCONCLUSIVE" and observed.findings == ()


def test_explanation_input_copy_cannot_replace_authoritative_result():
    results = [execution()]
    original_text = results[0].payload.resolved.contexts[0].content
    class MutatingAgent:
        def observe_knowledge_round(self, plan, supplied):
            # 模拟调用方代码误改模型嵌套对象；系统校验仍须对照原输入，而非此副本。
            supplied[0].payload.resolved.contexts[0].supporting_hits[0].source.document_version = "forged"
            data = draft(supplied)
            return KnowledgeObservationDraft.model_validate(data)
    output = asyncio.run(nodes.observation(current(results), investigation_agent=MutatingAgent(),
        execution_context=KnowledgeExecutionContext(actor_id="a", index_profile=IndexProfile.PARENT_CHILD)))
    assert output["observation"].error.code == "OBSERVATION_INVALID"
    assert results[0].payload.resolved.contexts[0].content == original_text
    assert results[0].payload.retrieved.hits[0].hit.source.document_version == "v1"
