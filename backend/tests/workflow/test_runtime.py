"""8B：共享图按次隔离身份，模型调用前确定性校验案件/范围/授权。"""

import asyncio

import pytest
from workflow_test_helpers import run_business_nodes

from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.knowledge.context_resolution import ContextResolutionResult
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult
from risk_platform.knowledge.serving_contracts import RetrievalRouteSnapshot
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, InvestigationTask, UserQuery
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord
from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationAction, InvestigationRuntimeContext, check_runtime


def make_runtime(planning_input, execution=None, *, actor="actor", investigation_id="I", round_id="R",
                 authorize_resource=lambda *_: True, authorize_knowledge=lambda *_: True, allow_unscoped=False):
    model_id = planning_input.monitoring_alert.model_id if planning_input.monitoring_alert else planning_input.user_query.model_id
    execution = execution or KnowledgeExecutionContext(actor_id=actor, index_profile="baseline-v1", scope={"model_id": model_id})
    return InvestigationRuntimeContext(
        investigation=InvestigationRecord(investigation_id=investigation_id, created_by="creator",
            planning_input=planning_input.model_copy(deep=True)),
        round=RoundRecord(investigation_id=investigation_id, round_id=round_id, round_number=1), actor_id=execution.actor_id, timeout_seconds=execution.timeout_seconds,
        authorize_resource=authorize_resource,
        tools=(KnowledgeSearchTool(execution, authorize_knowledge, allow_unscoped),))


def planning(model="M"):
    return InitialPlanningInput(user_query=UserQuery(query="调查异常", model_id=model),
                                 available_capabilities=["KNOWLEDGE_RETRIEVAL"])


@pytest.mark.parametrize("permission", [False, None, 1, "yes"])
def test_resource_authorization_requires_explicit_true(permission):
    with pytest.raises(InvestigationAccessError):
        check_runtime(make_runtime(planning(), authorize_resource=lambda *_: permission), InvestigationAction.CONTINUE)


def test_read_is_not_continue_and_policy_errors_do_not_leak():
    context = make_runtime(planning(), authorize_resource=lambda actor, action, resource: action == "investigation:read")
    check_runtime(context, InvestigationAction.READ)
    with pytest.raises(InvestigationAccessError):
        check_runtime(context, InvestigationAction.CONTINUE)
    def broken(*_):
        raise RuntimeError("secret-provider-body")
    with pytest.raises(InvestigationAccessError) as failure:
        check_runtime(make_runtime(planning(), authorize_resource=broken), InvestigationAction.READ)
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("scope", [{}, {"model_id": "other"}])
def test_case_model_requires_exact_scope(scope):
    execution = KnowledgeExecutionContext(actor_id="A", index_profile="baseline-v1", scope=scope)
    with pytest.raises(InvestigationAccessError):
        check_runtime(make_runtime(planning(), execution), InvestigationAction.CONTINUE)


def test_unscoped_case_requires_explicit_application_choice():
    with pytest.raises(InvestigationAccessError):
        check_runtime(make_runtime(planning(None)), InvestigationAction.READ)
    check_runtime(make_runtime(planning(None), allow_unscoped=True), InvestigationAction.READ)


@pytest.mark.parametrize("concurrent", [False, True])
def test_business_nodes_handle_two_actors_without_identity_or_scope_leak(monkeypatch, concurrent):
    from risk_platform.execution import knowledge
    observed = []
    class Agent:
        def initial_plan(self, source):
            model = source.user_query.model_id
            return InvestigationPlan(goal="查历史", tasks=[InvestigationTask(task_id=f"T-{model}", objective=model,
                capability="KNOWLEDGE_RETRIEVAL", expected_result="线索")])
        def formulate_knowledge_query(self, task, source, **_):
            assert task.objective == source.user_query.model_id
            return {"query": f"模型{task.objective}的历史案例", "top_k": 1}
    async def retrieve(request, **options):
        await asyncio.sleep(0)
        return DenseChildRetrievalResult(snapshot=RetrievalRouteSnapshot(index_profile=options["index_profile"],
            collection="physical-" + request.filters.model_id, route_version=1), hits=())
    async def resolve(result, **_):
        return ContextResolutionResult(snapshot=result.snapshot, contexts=())
    monkeypatch.setattr(knowledge, "retrieve_dense_children", retrieve)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
    agent = Agent()
    def auth(actor, action, execution):
        observed.append((actor, execution.scope.model_id))
        return True
    async def call(actor, model):
        source = planning(model)
        return await run_business_nodes(agent, source, make_runtime(source, actor=actor,
            investigation_id="I-"+actor, round_id="R-"+actor, authorize_knowledge=auth))
    async def run():
        return await asyncio.gather(call("A", "M1"), call("B", "M2")) if concurrent else [await call("A", "M1"), await call("B", "M2")]
    results = asyncio.run(run())
    assert sorted(observed) == [("A", "M1"), ("B", "M2")]
    for model, result in zip(("M1", "M2"), results, strict=True):
        assert result["task_execution_results"][0].payload.request.filters.model_id == model
        assert result["investigation_status"] == "OPEN" and result["round_complete"] is True
        assert "execution_context" not in result and "actor_id" not in result


@pytest.mark.parametrize("bad", ["missing", "scope", "source", "read-only"])
def test_invalid_runtime_never_calls_planning_or_query_model(bad):
    class Agent:
        def initial_plan(self, *_):
            pytest.fail("无权限/错范围不应调用规划模型")
    source = planning()
    context = make_runtime(source)
    if bad == "missing":
        context = None
    elif bad == "scope":
        context = make_runtime(source, KnowledgeExecutionContext(actor_id="A", index_profile="baseline-v1"))
    elif bad == "source":
        source = planning("other")
    else:
        context = make_runtime(source, authorize_resource=lambda _, action, __: action == "investigation:read")
    with pytest.raises(InvestigationAccessError):
        asyncio.run(run_business_nodes(Agent(), source, context))


def test_revocation_after_planning_stops_before_query_formulation():
    permission = {"allowed": True}
    class Agent:
        def initial_plan(self, _):
            permission["allowed"] = False
            return InvestigationPlan(goal="查历史", tasks=[InvestigationTask(task_id="T", objective="查历史",
                capability="KNOWLEDGE_RETRIEVAL", expected_result="线索")])
        def formulate_knowledge_query(self, *_args, **_kwargs):
            pytest.fail("权限撤销后不能生成 Query")
    source = planning()
    context = make_runtime(source, authorize_resource=lambda *_: permission["allowed"])
    with pytest.raises(InvestigationAccessError):
        asyncio.run(run_business_nodes(Agent(), source, context))
