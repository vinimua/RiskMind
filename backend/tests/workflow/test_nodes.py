from risk_platform.workflow import nodes
from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
    UserQuery,
)


class FakeInvestigationAgent:
    def __init__(self, plan: InvestigationPlan):
        self.plan = plan
        self.received_inputs: list[InitialPlanningInput] = []

    def initial_plan(self, planning_input: InitialPlanningInput) -> InvestigationPlan:
        self.received_inputs.append(planning_input)
        return self.plan


def test_initial_planning_delegates_to_investigation_agent() -> None:
    planning_input = InitialPlanningInput(
        user_query=UserQuery(query="为什么 KS 下降？", model_id="credit_v3"),
        available_capabilities=list(CapabilityType),
    )
    plan = InvestigationPlan(
        goal="建立调查背景",
        tasks=[
            InvestigationTask(
                objective="检索历史案例",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="获得历史调查经验",
            )
        ],
    )
    agent = FakeInvestigationAgent(plan)

    result = nodes.initial_planning(
        {"planning_input": planning_input},
        investigation_agent=agent,
    )

    assert agent.received_inputs == [planning_input]
    assert result == {"investigation_plan": plan}
