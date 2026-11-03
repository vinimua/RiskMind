from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.contracts import InitialPlanningInput
from risk_platform.workflow.runtime import InvestigationAction, check_runtime
from risk_platform.workflow.knowledge_tool import knowledge_execution, knowledge_authorizer
from risk_platform.workflow.nodes import (
    observation,
    replanning,
    task_execution,
)
from risk_platform.workflow.state import InvestigationState
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation


def _route_after_replanning(
    state: InvestigationState,
) -> Literal["continue", "round_complete"]:

    # END 只是结束本次图调用，不代表关闭案件。
    return "round_complete" if state.get("round_complete", False) else "continue"


def build_investigation_graph(
    investigation_agent: InvestigationAgent, *,
    session_factory, checkpointer, embedding_client=None, milvus_client=None,
):
    """持久化 runner 内部使用的唯一拓扑，不提供无账本执行入口。"""
    if session_factory is None or checkpointer is None:
        raise ValueError("正式执行必须配置账本会话与检查点存储")

    def invocation(runtime):
        # 声明类型与实际调用一致，不再兼容裸 InvestigationRuntimeContext。
        return require_invocation(runtime.context, claimed=True)

    def wire_update(update, call):
        from risk_platform.workflow.recovery import _wire
        return _wire(update)

    def phase_executor(call):
        from risk_platform.workflow.recovery import ledger_phase_executor
        return ledger_phase_executor(session_factory, call.recovery)

    async def plan_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = invocation(runtime)
        from risk_platform.workflow.recovery import prepare_plan
        return await prepare_plan(state, call.recovery, sessions=session_factory, agent=investigation_agent)

    async def execute_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = invocation(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        trusted = knowledge_execution(call.runtime, InvestigationAction.CONTINUE, source)
        update = await task_execution(state, investigation_agent=investigation_agent, execution_context=trusted,
                                    authorize=knowledge_authorizer(call.runtime), session_factory=session_factory,
                                    embedding_client=embedding_client, milvus_client=milvus_client,
                                    before_task=lambda: check_runtime(call.runtime, InvestigationAction.CONTINUE, source),
                                    execute_phase=phase_executor(call))
        return wire_update(update, call)

    async def observe_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = invocation(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        trusted = knowledge_execution(call.runtime, InvestigationAction.CONTINUE, source)
        update = await observation(state, investigation_agent=investigation_agent, execution_context=trusted,
                                 execute_phase=phase_executor(call))
        return wire_update(update, call)

    graph = StateGraph(InvestigationState, context_schema=InvestigationInvocation)
    graph.add_node("prepare_plan", plan_node)
    graph.add_node("task_execution", execute_node)
    graph.add_node("observation", observe_node)
    graph.add_node("replanning", replanning)

    graph.add_edge(START, "prepare_plan")
    graph.add_edge("prepare_plan", "task_execution")
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

    return graph.compile(checkpointer=checkpointer)
