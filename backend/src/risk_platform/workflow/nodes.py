"""Deterministic placeholder nodes for the V0 investigation workflow."""

from risk_platform.workflow.state import InvestigationState


def initial_planning(_: InvestigationState) -> dict[str, object]:
    """Enter the planning phase."""

    return {"phase": "PLANNING"}


def task_execution(_: InvestigationState) -> dict[str, object]:
    """Enter the task execution phase."""

    return {"phase": "INVESTIGATING"}


def observation(_: InvestigationState) -> dict[str, object]:
    """Enter the observation phase."""

    return {"phase": "OBSERVING"}


def replanning(_: InvestigationState) -> dict[str, object]:
    """Enter replanning and end the V0 investigation path."""

    # Temporary V0 skeleton behavior: real replanning will decide whether to
    # continue the investigation after later workflow chapters are implemented.
    return {
        "phase": "REPLANNING",
        "investigation_complete": True,
    }
