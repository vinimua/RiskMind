"""Minimal state contract for the V0 investigation workflow."""

from typing import NotRequired, TypedDict

from risk_platform.workflow.contracts import InvestigationPlan


class InvestigationState(TypedDict):
    """State shared by the nodes in the investigation skeleton."""

    query: str
    investigation_plan: NotRequired[InvestigationPlan]
    investigation_complete: NotRequired[bool]
