"""Nodes for the V0 investigation workflow."""

import os

from openai import OpenAI

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.state import InvestigationState

_ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY")
if not _ZHIPU_API_KEY:
    raise RuntimeError("ZHIPU_API_KEY is not set")

investigation_agent = InvestigationAgent(
    client=OpenAI(
        api_key=_ZHIPU_API_KEY,
        base_url=os.environ.get(
            "ZHIPU_BASE_URL",
            "https://open.bigmodel.cn/api/paas/v4/",
        ),
    ),
    model=os.environ.get("ZHIPU_MODEL", "glm-4.7-flash"),
)


def initial_planning(state: InvestigationState) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    plan = investigation_agent.initial_plan(state["planning_input"])

    return {"investigation_plan": plan}


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
