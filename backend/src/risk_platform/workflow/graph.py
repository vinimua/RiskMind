from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.runtime import InvestigationAction, InvestigationRuntimeContext, check_runtime, knowledge_authorizer
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
    investigation_agent: InvestigationAgent, *,
    session_factory=None, embedding_client=None, milvus_client=None,
):
    """共享图仅绑定服务依赖；每次 ainvoke 必须提供独立可信 context。"""

    async def plan_node(state: InvestigationState, runtime: Runtime[InvestigationRuntimeContext]):
        check_runtime(runtime.context, InvestigationAction.CONTINUE, state["planning_input"])
        return initial_planning({**state, "planning_input": state["planning_input"].model_copy(deep=True)},
                                investigation_agent=investigation_agent)

    async def execute_node(state: InvestigationState, runtime: Runtime[InvestigationRuntimeContext]):
        trusted = check_runtime(runtime.context, InvestigationAction.CONTINUE, state["planning_input"])
        return await task_execution(state, investigation_agent=investigation_agent, execution_context=trusted,
                                    authorize=knowledge_authorizer(runtime.context), session_factory=session_factory,
                                    embedding_client=embedding_client, milvus_client=milvus_client,
                                    before_task=lambda: check_runtime(runtime.context, InvestigationAction.CONTINUE, state["planning_input"]))

    async def observe_node(state: InvestigationState, runtime: Runtime[InvestigationRuntimeContext]):
        trusted = check_runtime(runtime.context, InvestigationAction.CONTINUE, state["planning_input"])
        return await observation(state, investigation_agent=investigation_agent, execution_context=trusted)

    graph = StateGraph(InvestigationState, context_schema=InvestigationRuntimeContext)
    graph.add_node("initial_planning", plan_node)
    graph.add_node("task_execution", execute_node)
    graph.add_node("observation", observe_node)
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
