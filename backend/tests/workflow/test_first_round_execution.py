"""7B：显式查询构造、顺序执行、7A 授权与任务关联；不实现 Observation。"""

import asyncio

import pytest

from risk_platform.execution import knowledge
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.knowledge.context_resolution import ContextResolutionResult, ContextResolutionIntegrityError
from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot
from risk_platform.workflow.contracts import CapabilityType, InitialPlanningInput, InvestigationPlan, InvestigationTask, UserQuery
from risk_platform.workflow.graph import build_investigation_graph
from risk_platform.workflow.nodes import task_execution


def state(count=2):
    return {
        "planning_input": InitialPlanningInput(user_query=UserQuery(query="为什么指标下降？", model_id="M"),
                                               available_capabilities=[CapabilityType.KNOWLEDGE_RETRIEVAL]),
        "investigation_plan": InvestigationPlan(goal="调查背景", tasks=[InvestigationTask(
            task_id=f"T{i}", objective=f"调查目标{i}", expected_result="带来源知识",
            capability=CapabilityType.KNOWLEDGE_RETRIEVAL) for i in range(count)]),
    }


class Agent:
    def __init__(self, current, events, failure=None):
        self.current, self.events, self.failure = current, events, failure

    def initial_plan(self, _):
        return self.current["investigation_plan"]

    def formulate_knowledge_query(self, task, planning_input, *, goal, max_top_k):
        self.events.append(("formulate", task.task_id))
        assert goal == "调查背景" and planning_input.user_query.model_id == "M"
        assert max_top_k == 5
        if task.task_id == "T0" and self.failure:
            raise self.failure
        return {"query": f"具体检索问题{task.task_id}", "filters": {}, "top_k": 2}


def context():
    return KnowledgeExecutionContext(actor_id="trusted", index_profile=IndexProfile.BASELINE,
                                     scope=KnowledgeFilters(model_id="M"), max_top_k=5)


def empty_dependencies(monkeypatch, events, fail_resolution=False):
    snapshot = RetrievalRouteSnapshot(index_profile=IndexProfile.BASELINE, collection="physical", route_version=1)
    async def retrieve(request, **options):
        assert request.filters.model_id == "M"
        assert request.query.startswith("具体检索问题") and not request.query.startswith("调查目标")
        events.append(("retrieve", request.query[-2:]))
        await asyncio.sleep(0)
        return DenseChildRetrievalResult(snapshot=snapshot, hits=())
    async def resolve(result, **_):
        events.append(("resolve", ""))
        if fail_resolution:
            raise ContextResolutionIntegrityError("private")
        return ContextResolutionResult(snapshot=result.snapshot, contexts=())
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)


def test_real_7a_path_sequential_zero_hits_and_task_association(monkeypatch):
    original, events = state(), []
    empty_dependencies(monkeypatch, events)
    result = asyncio.run(task_execution(original, investigation_agent=Agent(original, events),
                                        execution_context=context(), authorize=lambda *_: True))
    assert [e[0] for e in events] == ["formulate", "retrieve", "resolve", "formulate", "retrieve", "resolve"]
    assert [r.task_id for r in result["task_execution_results"]] == ["T0", "T1"]
    assert all(r.status == "SUCCEEDED" and r.payload.resolved.contexts == () for r in result["task_execution_results"])
    assert all(t.status == "COMPLETED" for t in result["investigation_plan"].tasks)
    assert all(t.status == "PENDING" for t in original["investigation_plan"].tasks)
    assert len({r.execution_id for r in result["task_execution_results"]}) == 2


@pytest.mark.parametrize("failure,code", [(ValueError("secret"), "QUERY_FORMULATION_INVALID"),
                                         (TimeoutError(), "QUERY_FORMULATION_TIMEOUT"),
                                         (RuntimeError("secret"), "QUERY_FORMULATION_FAILED")])
def test_formulation_failure_no_tool_and_next_task_continues(monkeypatch, failure, code):
    original, events = state(), []
    empty_dependencies(monkeypatch, events)
    result = asyncio.run(task_execution(original, investigation_agent=Agent(original, events, failure),
                                        execution_context=context(), authorize=lambda *_: True))
    first, second = result["task_execution_results"]
    assert first.error.code == code and first.payload is None
    assert "secret" not in first.model_dump_json()
    assert second.status == "SUCCEEDED"
    assert [e[0] for e in events] == ["formulate", "formulate", "retrieve", "resolve"]


def test_candidate_cannot_supply_system_identity():
    original, events = state(1), []
    agent = Agent(original, events)
    agent.formulate_knowledge_query = lambda *_, **__: {"query": "q", "top_k": 1, "actor_id": "forged"}
    result = asyncio.run(task_execution(original, investigation_agent=agent,
                                        execution_context=context(), authorize=lambda *_: pytest.fail("非法请求不应鉴权")))
    assert result["task_execution_results"][0].status == "REJECTED"
    assert result["investigation_plan"].tasks[0].status == "FAILED"


def test_full_graph_success_remains_open_and_resets_old_results(monkeypatch):
    original, events = state(), []
    empty_dependencies(monkeypatch, events)
    graph = build_investigation_graph(Agent(original, events), execution_context=context(), authorize=lambda *_: True)
    result = asyncio.run(graph.ainvoke({"planning_input": original["planning_input"],
                                      "round_complete": True, "task_execution_results": []}))
    assert result["round_complete"] is True and result["investigation_status"] == "OPEN"
    assert len(result["task_execution_results"]) == 2
    assert "observation" not in result and "hypotheses" not in result
    assert all(t.status == "COMPLETED" for t in result["investigation_plan"].tasks)


def test_integrity_failure_is_failed_not_empty_success(monkeypatch):
    original, events = state(1), []
    empty_dependencies(monkeypatch, events, fail_resolution=True)
    result = asyncio.run(task_execution(original, investigation_agent=Agent(original, events),
                                        execution_context=context(), authorize=lambda *_: True))
    assert result["task_execution_results"][0].error.category == "INTEGRITY"
    assert result["investigation_plan"].tasks[0].status == "FAILED"


def test_duplicate_ids_rejected_before_query():
    original, events = state(), []
    original["investigation_plan"].tasks[1].task_id = "T0"
    with pytest.raises(ValueError, match="唯一"):
        asyncio.run(task_execution(original, investigation_agent=Agent(original, events),
                                   execution_context=context(), authorize=lambda *_: True))
    assert not events


def test_nonknowledge_task_rejected_without_query():
    original, events = state(1), []
    original["investigation_plan"].tasks[0].capability = CapabilityType.COMPUTATION
    result = asyncio.run(task_execution(original, investigation_agent=Agent(original, events),
                                        execution_context=context(), authorize=lambda *_: True))
    assert not events and result["task_execution_results"][0].error.code == "UNSUPPORTED_CAPABILITY"


def test_cancellation_propagates():
    original, events = state(1), []
    agent = Agent(original, events, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(task_execution(original, investigation_agent=agent, execution_context=context(), authorize=lambda *_: True))


def test_formulation_deadline_does_not_execute_tool():
    import time
    original, events = state(1), []
    agent = Agent(original, events)
    def slow(*_, **__):
        time.sleep(.04)
        return {"query": "q", "top_k": 1}
    agent.formulate_knowledge_query = slow
    trusted = context().model_copy(update={"timeout_seconds": .001})
    result = asyncio.run(task_execution(original, investigation_agent=agent, execution_context=trusted,
                                        authorize=lambda *_: pytest.fail("查询构造超时不得执行工具")))
    assert result["task_execution_results"][0].error.code == "QUERY_FORMULATION_TIMEOUT"


def test_wrong_task_result_identity_not_written_to_state(monkeypatch):
    from datetime import datetime, timezone
    from risk_platform.execution.contracts import TaskExecutionResult, ExecutionError
    from risk_platform.workflow import nodes
    original, events = state(1), []
    async def wrong(*_, **__):
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="E", task_id="OTHER", capability="KNOWLEDGE_RETRIEVAL",
                                   status="FAILED", started_at=now, completed_at=now,
                                   error=ExecutionError(category="INTERNAL", code="X", message="错误", retryable=False))
    monkeypatch.setattr(nodes, "execute_knowledge_search", wrong)
    with pytest.raises(ValueError, match="身份不一致"):
        asyncio.run(task_execution(original, investigation_agent=Agent(original, events),
                                   execution_context=context(), authorize=lambda *_: True))
    assert "task_execution_results" not in original
