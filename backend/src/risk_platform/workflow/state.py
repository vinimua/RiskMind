"""Minimal state contract for the V0 investigation workflow."""

from typing import Literal, NotRequired, TypedDict


InvestigationPhase = Literal[
    "PLANNING",
    "INVESTIGATING",
    "OBSERVING",
    "REPLANNING",
]


class InvestigationState(TypedDict):
    """State shared by the nodes in the investigation skeleton."""

    query: str
    phase: NotRequired[InvestigationPhase]
    investigation_complete: NotRequired[bool]
