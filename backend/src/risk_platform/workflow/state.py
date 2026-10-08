"""Minimal state contract for the V0 investigation workflow."""

from typing import NotRequired, TypedDict


class InvestigationState(TypedDict):
    """State shared by the nodes in the investigation skeleton."""

    query: str
    investigation_needs: NotRequired[list[str]]
    investigation_complete: NotRequired[bool]
