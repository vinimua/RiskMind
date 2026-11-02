"""Nodes for the V0 investigation workflow."""

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.state import InvestigationState


def initial_planning(
    state: InvestigationState,
    *,
    investigation_agent: InvestigationAgent,
) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    plan = investigation_agent.initial_plan(state["planning_input"])

    # 新轮次清除旧的结束标记，案件保持开放。
    return {"investigation_plan": plan, "round_complete": False, "investigation_status": "OPEN"}


def task_execution(_: InvestigationState) -> dict[str, object]:
    """Run the V0 task-execution placeholder."""

    return {}


def observation(_: InvestigationState) -> dict[str, object]:
    """Run the V0 observation placeholder."""

    return {}


def replanning(_: InvestigationState) -> dict[str, object]:
    """结束本轮 V0 骨架遍历；不表示案件调查完成，也不执行真实重规划。"""

    # Temporary V0 skeleton behavior: real replanning will decide whether to
    # continue the investigation after later workflow chapters are implemented.
    return {"round_complete": True, "investigation_status": "OPEN"}
