from functools import partial
from collections.abc import Callable
from typing import Literal

from langgraph.graph import END, START, StateGraph

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.workflow.nodes import (
    initial_planning,
    observation,
    replanning,
    task_execution,
)
from risk_platform.workflow.state import InvestigationState


def _route_after_replanning(
    state: InvestigationState,
) -> Literal["continue", "round_complete"]:

    # END 只是结束本次图调用，不代表关闭案件。
    return "round_complete" if state.get("round_complete", False) else "continue"


def build_investigation_graph(
    investigation_agent: InvestigationAgent, *, execution_context: KnowledgeExecutionContext,
    authorize: Callable[[str, str, KnowledgeExecutionContext], bool],
    session_factory=None, embedding_client=None, milvus_client=None,
):
    """由可信应用注入执行身份、范围与鉴权策略，使用 ainvoke 执行异步知识节点。"""

    graph = StateGraph(InvestigationState)
    graph.add_node(
        "initial_planning",
        partial(initial_planning, investigation_agent=investigation_agent),
    )
    graph.add_node("task_execution", partial(
        task_execution, investigation_agent=investigation_agent,
        execution_context=execution_context, authorize=authorize,
        session_factory=session_factory, embedding_client=embedding_client, milvus_client=milvus_client,
    ))
    graph.add_node("observation", partial(observation, investigation_agent=investigation_agent,
                                          execution_context=execution_context))
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
            "round_complete": END,
        },
    )

    return graph.compile()
