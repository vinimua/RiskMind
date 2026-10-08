
from typing import Literal

from langgraph.graph import END, START, StateGraph

from risk_platform.workflow.nodes import (
    initial_planning,
    observation,
    replanning,
    task_execution,
)
from risk_platform.workflow.state import InvestigationState


def _route_after_replanning(
    state: InvestigationState,
) -> Literal["continue", "complete"]:

    return "complete" if state.get("investigation_complete", False) else "continue"


def build_investigation_graph():

    graph = StateGraph(InvestigationState)
    graph.add_node("initial_planning", initial_planning)
    graph.add_node("task_execution", task_execution)
    graph.add_node("observation", observation)
    graph.add_node("replanning", replanning)

    graph.add_edge(START, "initial_planning")
    graph.add_edge("initial_planning", "task_execution")
    graph.add_edge("task_execution", "observation")
    graph.add_edge("observation", "replanning")
    graph.add_conditional_edges(
        "replanning",
        _route_after_replanning,
        {
            "continue": "task_execution",
            "complete": END,
        },
    )

    return graph.compile()
