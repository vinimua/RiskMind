import asyncio
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from workflow_test_helpers import run_business_nodes
from risk_platform.workflow.runtime import InvestigationAccessError

from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    UserQuery,
)
from risk_platform.workflow.graph import _route_after_replanning, build_investigation_graph
from risk_platform.workflow.nodes import initial_planning, replanning
from risk_platform.workflow.invocation import InvestigationInvocation
from test_runtime import make_runtime


class FakeInvestigationAgent:
    def __init__(self, plan: InvestigationPlan):
        self.plan = plan

    def initial_plan(self, _: InitialPlanningInput) -> InvestigationPlan:
        return self.plan

    def formulate_knowledge_query(self, *_, **__):
        return {"query": "历史标签成熟度导致指标低估的案例", "top_k": 3}


def _graph(agent):
    return build_investigation_graph(agent, session_factory=object(), checkpointer=InMemorySaver())


def _plan() -> InvestigationPlan:
    return InvestigationPlan(
        goal="建立调查背景",
        tasks=[
            InvestigationTask(
                objective="检索历史案例",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="获得历史调查经验",
            )
        ],
    )


def _planning_input() -> InitialPlanningInput:
    return InitialPlanningInput(
        user_query=UserQuery(query="为什么 KS 下降？", model_id="credit_v3"),
        available_capabilities=list(CapabilityType),
    )


def test_investigation_graph_compiles() -> None:
    graph = _graph(FakeInvestigationAgent(_plan()))

    assert graph is not None


@pytest.mark.parametrize("missing", ["session_factory", "checkpointer"])
def test_graph_requires_persistence_dependencies(missing):
    dependencies = {"session_factory": object(), "checkpointer": InMemorySaver()}
    dependencies[missing] = None
    with pytest.raises(ValueError, match="账本会话与检查点"):
        build_investigation_graph(FakeInvestigationAgent(_plan()), **dependencies)


def test_single_persistent_topology() -> None:
    graph = _graph(FakeInvestigationAgent(_plan()))
    assert set(graph.get_graph().nodes) == {
        "__start__", "prepare_plan", "task_execution", "observation", "replanning", "__end__",
    }
    # 恢复模块只能提供执行机制，不能再构造自己的 StateGraph。
    import ast
    import inspect
    from risk_platform.workflow import recovery
    tree = ast.parse(inspect.getsource(recovery))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "StateGraph" for node in ast.walk(tree))
    assert not hasattr(recovery, "RecoveryState")


def test_state_cannot_enable_retired_direct_execution() -> None:
    source = _planning_input()
    graph = _graph(FakeInvestigationAgent(_plan()))
    runtime = make_runtime(source, authorize_knowledge=lambda *_: False)
    with pytest.raises(InvestigationAccessError):
        asyncio.run(graph.ainvoke({
            "planning_input": source, "mode": "RECOVERY", "run_id": "untrusted",
            "plan_id": "untrusted", "checkpoint_schema_version": 2,
        }, config={"configurable": {"thread_id": "rejected-direct"}},
           context=InvestigationInvocation(runtime=runtime)))


def test_round_ends_but_investigation_remains_open() -> None:
    planning_input = _planning_input()
    plan = _plan()
    graph = _graph(FakeInvestigationAgent(plan))

    result = asyncio.run(run_business_nodes(FakeInvestigationAgent(plan), planning_input,
        make_runtime(planning_input, actor="operator", authorize_knowledge=lambda *_: False)))

    assert result["planning_input"] == planning_input
    assert result["round_complete"] is True and result["investigation_status"] == "OPEN"
    assert all(task.status == "FAILED" for task in result["investigation_plan"].tasks)
    assert result["task_execution_results"][0].status == "REJECTED"
    assert result["task_execution_results"][0].error.code == "TOOL_NOT_AUTHORIZED"
    assert plan.tasks[0].status == "PENDING"  # Graph 不原地修改 Agent 返回的计划。
    assert "investigation_complete" not in result


def test_new_round_resets_previous_round_end_flag() -> None:
    state = {"planning_input": _planning_input(), "round_complete": True, "investigation_status": "OPEN"}
    update = initial_planning(state, investigation_agent=FakeInvestigationAgent(_plan()))
    assert update["round_complete"] is False
    assert update["investigation_status"] == "OPEN"
    assert update["task_execution_results"] == []


def test_routing_uses_round_end_not_case_completion() -> None:
    assert _route_after_replanning({"round_complete": True, "investigation_status": "OPEN"}) == "round_complete"
    assert _route_after_replanning({"round_complete": False, "investigation_status": "OPEN"}) == "continue"
    assert _route_after_replanning({"investigation_complete": True}) == "continue"
    assert replanning({}) == {"round_complete": True, "investigation_status": "OPEN"}
