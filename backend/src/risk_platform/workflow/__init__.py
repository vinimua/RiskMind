"""Investigation workflow orchestration."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from risk_platform.workflow.graph import build_investigation_graph
    from risk_platform.workflow.state import InvestigationState

__all__ = ["InvestigationState", "build_investigation_graph"]


def __getattr__(name: str):
    if name == "build_investigation_graph":
        from risk_platform.workflow.graph import build_investigation_graph

        return build_investigation_graph
    if name == "InvestigationState":
        from risk_platform.workflow.state import InvestigationState

        return InvestigationState
    raise AttributeError(name)
