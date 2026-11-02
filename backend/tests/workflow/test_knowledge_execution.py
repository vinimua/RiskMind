"""Stage 7A 的执行边界；不启动 LangGraph，不调用真实模型或数据库。"""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from risk_platform.execution import knowledge
from risk_platform.execution.contracts import (
    ExecutionError, ExecutionStatus, KnowledgeExecutionContext,
    KnowledgeExecutionPayload, KnowledgeSearchToolRequest, TaskExecutionResult,
)
from risk_platform.knowledge.context_resolution import ContextResolutionResult, ContextResolutionIntegrityError
from risk_platform.knowledge.contracts import KnowledgeFilters, KnowledgeHit, KnowledgeSourceRef
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult, RetrievedKnowledgeHit, ChildRetrievalIntegrityError
from risk_platform.knowledge.serving import MissingServingRouteError
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot
from risk_platform.workflow.contracts import CapabilityType, InvestigationTask


def task():
    return InvestigationTask(task_id="task-1", objective="查找历史经验", expected_result="带来源的知识",
                             capability=CapabilityType.KNOWLEDGE_RETRIEVAL)


def context(**changes):
    return KnowledgeExecutionContext(actor_id="operator", index_profile=IndexProfile.BASELINE, **changes)


def outputs(profile=IndexProfile.BASELINE, empty=False):
    snapshot = RetrievalRouteSnapshot(index_profile=profile, collection="physical", route_version=1)
    hit = KnowledgeHit(source=KnowledgeSourceRef(document_id="D", document_version="v1", chunk_id="C"),
                       document_type="INCIDENT_REPORT", title="历史报告", section_path="原因", content="历史经验", rank=1)
    retrieved = DenseChildRetrievalResult(snapshot=snapshot, hits=() if empty else (RetrievedKnowledgeHit(hit=hit, score=.8),))
    resolved = ContextResolutionResult(snapshot=snapshot, contexts=() if empty else (
        KnowledgeContext(context_id="C" if profile==IndexProfile.BASELINE else "P", content="历史上下文", supporting_hits=(hit,)),))
    return retrieved, resolved


def run(candidate=None, ctx=None, **kwargs):
    return asyncio.run(knowledge.execute_knowledge_search(
        task(), {"query": "历史原因", "top_k": 3} if candidate is None else candidate,
        context=ctx or context(), authorize=kwargs.pop("authorize", lambda *_: True), **kwargs))


@pytest.mark.parametrize("profile", list(IndexProfile))
@pytest.mark.parametrize("empty", [False, True])
def test_success_and_zero_hits_preserve_lineage(monkeypatch, profile, empty):
    retrieved, resolved = outputs(profile, empty)
    calls = []
    async def retrieve(request, **options):
        calls.append((request, options))
        return retrieved
    async def resolve(result, **options):
        assert result == retrieved
        return resolved
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
    result = run(ctx=KnowledgeExecutionContext(actor_id="operator", index_profile=profile))
    assert result.status == ExecutionStatus.SUCCEEDED and result.error is None
    assert result.payload.retrieved == retrieved and result.payload.resolved == resolved
    assert result.task_id == "task-1" and result.execution_id.startswith("exec-")
    assert result.started_at.tzinfo and result.completed_at >= result.started_at
    assert calls[0][1]["index_profile"] == profile


@pytest.mark.parametrize("candidate", [
    {"query": " "}, {"query": "q", "top_k": True}, {"query": "q", "top_k": 0},
    {"query": "q", "top_k": 21}, {"query": "q", "execution_id": "forged"},
    {"query": "q", "index_profile": "baseline-v1"}, {"query": "q", "collection": "forged"},
    {"query": "q", "filters": {"model_id": "OTHER"}},
])
def test_invalid_or_out_of_scope_request_rejected_before_permission(candidate):
    def forbidden(*_):
        pytest.fail("非法请求不得进入权限检查或检索")
    result = run(candidate, context(scope=KnowledgeFilters(model_id="M")), authorize=forbidden)
    assert result.status == ExecutionStatus.REJECTED
    assert result.error.category == "VALIDATION" and result.payload is None


def test_scope_applied_and_candidate_not_mutated(monkeypatch):
    candidate = {"query": "q", "top_k": 3, "filters": {}}
    async def retrieve(request, **_):
        assert request.filters.model_id == "M"
        assert request.filters.document_types == ["INCIDENT_REPORT"]
        return outputs(empty=True)[0]
    async def resolve(*_, **__):
        return outputs(empty=True)[1]
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
    result = run(candidate, context(scope=KnowledgeFilters(model_id="M", document_types=["INCIDENT_REPORT"])))
    assert result.status == "SUCCEEDED" and candidate["filters"] == {}
    assert result.payload.request.filters.model_id == "M"


@pytest.mark.parametrize("allowed", [False, None, 1, "yes"])
def test_authorization_requires_explicit_true(allowed):
    result = run(authorize=lambda *_: allowed)
    assert result.status == "REJECTED" and result.error.code == "TOOL_NOT_AUTHORIZED"


@pytest.mark.parametrize("failure,category,code,retryable", [
    (TimeoutError(), "DEPENDENCY", "KNOWLEDGE_SEARCH_TIMEOUT", True),
    (ConnectionError("secret-key"), "DEPENDENCY", "KNOWLEDGE_DEPENDENCY_FAILURE", True),
    (MissingServingRouteError("secret-key"), "DEPENDENCY", "SERVING_ROUTE_MISSING", False),
    (ChildRetrievalIntegrityError("secret-key"), "INTEGRITY", "KNOWLEDGE_INTEGRITY_FAILURE", False),
    (ContextResolutionIntegrityError("secret-key"), "INTEGRITY", "KNOWLEDGE_INTEGRITY_FAILURE", False),
    (RuntimeError("secret-key"), "INTERNAL", "KNOWLEDGE_EXECUTION_FAILURE", False),
])
def test_failure_categories_no_partial_payload_or_secret(monkeypatch, failure, category, code, retryable):
    async def retrieve(*_, **__):
        raise failure
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    result = run()
    assert result.status == "FAILED" and result.payload is None
    assert (result.error.category, result.error.code, result.error.retryable) == (category, code, retryable)
    assert "secret-key" not in result.model_dump_json()


def test_deadline_and_cancellation(monkeypatch):
    async def retrieve(*_, **__):
        await asyncio.sleep(10)
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    assert run(ctx=context(timeout_seconds=.01)).error.code == "KNOWLEDGE_SEARCH_TIMEOUT"
    async def cancelled(*_, **__):
        raise asyncio.CancelledError()
    monkeypatch.setattr(knowledge, "retrieve_dense_children", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run()


def test_wrong_capability_rejected():
    wrong = task().model_copy(update={"capability": CapabilityType.COMPUTATION})
    result = asyncio.run(knowledge.execute_knowledge_search(wrong, {"query": "q"}, context=context(), authorize=lambda *_: True))
    assert result.status == "REJECTED" and result.error.code == "UNSUPPORTED_CAPABILITY"


def test_payload_and_result_invariants():
    retrieved, resolved = outputs()
    payload = KnowledgeExecutionPayload(request=KnowledgeSearchToolRequest(query="q", top_k=1), retrieved=retrieved, resolved=resolved)
    now = datetime.now(timezone.utc)
    values = dict(execution_id="E", task_id="T", capability="KNOWLEDGE_RETRIEVAL", started_at=now, completed_at=now)
    with pytest.raises(ValidationError):
        TaskExecutionResult(**values, status="SUCCEEDED")
    with pytest.raises(ValidationError):
        TaskExecutionResult(**values, status="FAILED", payload=payload)
    with pytest.raises(ValidationError):
        TaskExecutionResult(**{**values, "completed_at": now-timedelta(seconds=1)}, status="SUCCEEDED", payload=payload)
    with pytest.raises(ValidationError):
        KnowledgeExecutionPayload(request=payload.request, retrieved=retrieved,
                                  resolved=ContextResolutionResult(snapshot=resolved.snapshot, contexts=()))
    with pytest.raises(ValidationError):
        ExecutionError(category="DEPENDENCY", code="X", message="错误", retryable="true")


def test_resolution_failure_has_no_partial_children(monkeypatch):
    async def retrieve(*_, **__):
        return outputs()[0]
    async def resolve(*_, **__):
        raise ContextResolutionIntegrityError("private upstream data")
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
    first, second = run(), run()
    assert first.payload is None and first.error.category == "INTEGRITY"
    assert first.execution_id != second.execution_id
    assert "private upstream data" not in first.model_dump_json()


def test_authorizer_exception_is_not_empty_success():
    def broken(*_):
        raise RuntimeError("private")
    result = run(authorize=broken)
    assert result.status == "FAILED" and result.error.code == "AUTHORIZATION_CHECK_FAILED"


def test_document_type_scope_cannot_be_widened():
    result = run({"query": "q", "top_k": 1, "filters": {"document_types": ["MODEL_DEVELOPMENT_REPORT"]}},
                 context(scope=KnowledgeFilters(document_types=["INCIDENT_REPORT"])))
    assert result.status == "REJECTED" and result.error.code == "INVALID_TOOL_REQUEST"
