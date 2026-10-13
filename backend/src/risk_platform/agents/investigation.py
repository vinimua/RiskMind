"""Investigation Agent interface."""

from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan


class InvestigationAgent:
    def initial_plan(
        self,
        planning_input: InitialPlanningInput,
    ) -> InvestigationPlan:
        """识别为调查当前异常，第一轮需要查什么。"""

        raise NotImplementedError
