"""Nodes for the V0 investigation workflow."""

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.state import InvestigationState

investigation_agent = InvestigationAgent()


def initial_planning(state: InvestigationState) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    result = investigation_agent.initial_plan(state["query"])

    return {"investigation_needs": result}


def task_execution(_: InvestigationState) -> dict[str, object]:
    """Run the V0 task-execution placeholder."""

    return {}


def observation(_: InvestigationState) -> dict[str, object]:
    """Run the V0 observation placeholder."""

    return {}


def replanning(_: InvestigationState) -> dict[str, object]:
    """Run replanning and end the V0 investigation path."""

    # Temporary V0 skeleton behavior: real replanning will decide whether to
    # continue the investigation after later workflow chapters are implemented.
    return {"investigation_complete": True}
