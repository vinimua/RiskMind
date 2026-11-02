from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    UserQuery,
)
from risk_platform.workflow.graph import _route_after_replanning, build_investigation_graph
from risk_platform.workflow.nodes import initial_planning, replanning


class FakeInvestigationAgent:
    def __init__(self, plan: InvestigationPlan):
        self.plan = plan

    def initial_plan(self, _: InitialPlanningInput) -> InvestigationPlan:
        return self.plan


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
    graph = build_investigation_graph(FakeInvestigationAgent(_plan()))

    assert graph is not None


def test_round_ends_but_investigation_remains_open() -> None:
    planning_input = _planning_input()
    plan = _plan()
    graph = build_investigation_graph(FakeInvestigationAgent(plan))

    result = graph.invoke({"planning_input": planning_input})

    assert result == {
        "planning_input": planning_input,
        "investigation_plan": plan,
        "round_complete": True,
        "investigation_status": "OPEN",
    }
    assert all(task.status == "PENDING" for task in result["investigation_plan"].tasks)
    assert "investigation_complete" not in result


def test_new_round_resets_previous_round_end_flag() -> None:
    state = {"planning_input": _planning_input(), "round_complete": True, "investigation_status": "OPEN"}
    update = initial_planning(state, investigation_agent=FakeInvestigationAgent(_plan()))
    assert update["round_complete"] is False
    assert update["investigation_status"] == "OPEN"


def test_routing_uses_round_end_not_case_completion() -> None:
    assert _route_after_replanning({"round_complete": True, "investigation_status": "OPEN"}) == "round_complete"
    assert _route_after_replanning({"round_complete": False, "investigation_status": "OPEN"}) == "continue"
    assert _route_after_replanning({"investigation_complete": True}) == "continue"
    assert replanning({}) == {"round_complete": True, "investigation_status": "OPEN"}
