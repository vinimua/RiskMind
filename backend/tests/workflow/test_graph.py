from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    UserQuery,
)
from risk_platform.workflow.graph import build_investigation_graph


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


def test_investigation_graph_runs_to_v0_completion() -> None:
    planning_input = _planning_input()
    plan = _plan()
    graph = build_investigation_graph(FakeInvestigationAgent(plan))

    result = graph.invoke({"planning_input": planning_input})

    assert result == {
        "planning_input": planning_input,
        "investigation_plan": plan,
        "investigation_complete": True,
    }
