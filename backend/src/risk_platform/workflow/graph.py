from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow.contracts import InitialPlanningInput
from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationAction, InvestigationRuntimeContext, check_runtime, knowledge_authorizer
from risk_platform.workflow.nodes import (
    initial_planning,
    observation,
    replanning,
    task_execution,
)
from risk_platform.workflow.state import InvestigationState
from risk_platform.workflow.invocation import InvestigationInvocation


def _route_after_replanning(
    state: InvestigationState,
) -> Literal["continue", "round_complete"]:

    # END 只是结束本次图调用，不代表关闭案件。
    return "round_complete" if state.get("round_complete", False) else "continue"


def build_investigation_graph(
    investigation_agent: InvestigationAgent, *,
    session_factory=None, embedding_client=None, milvus_client=None, checkpointer=None,
):
    """共享图仅绑定服务依赖；每次 ainvoke 必须提供独立可信 context。"""

    def invocation(runtime):
        # 保留 8B 普通调用的 context API；恢复模式只能由可信包装对象开启。
        value = runtime.context
        if isinstance(value, InvestigationRuntimeContext):
            return InvestigationInvocation(runtime=value)
        if not isinstance(value, InvestigationInvocation):
            raise InvestigationAccessError("调查图需要可信运行上下文")
        if value.recovery is not None and value.recovery.context is not value.runtime:
            raise InvestigationAccessError("恢复上下文与可信身份不一致")
        return value

    def wire_update(update, call):
        if call.recovery is None:
            return update
        from risk_platform.workflow.recovery import _wire
        return _wire(update)

    def phase_executor(call):
        if call.recovery is None:
            return None  # 兼容 7B/8B 非持久化入口，不自动接受计划或创建账本身份。
        from risk_platform.workflow.recovery import ledger_phase_executor
        return ledger_phase_executor(session_factory, call.recovery)

    async def plan_node(state: InvestigationState, runtime: Runtime):
        call = invocation(runtime)
        if call.recovery is not None:
            from risk_platform.workflow.recovery import load_accepted_plan
            return await load_accepted_plan(state, call.recovery, sessions=session_factory)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
        return initial_planning({**state, "planning_input": source.model_copy(deep=True)},
                                investigation_agent=investigation_agent)

    async def execute_node(state: InvestigationState, runtime: Runtime):
        call = invocation(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        trusted = check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
        update = await task_execution(state, investigation_agent=investigation_agent, execution_context=trusted,
                                    authorize=knowledge_authorizer(call.runtime), session_factory=session_factory,
                                    embedding_client=embedding_client, milvus_client=milvus_client,
                                    before_task=lambda: check_runtime(call.runtime, InvestigationAction.CONTINUE, source),
                                    execute_phase=phase_executor(call))
        return wire_update(update, call)

    async def observe_node(state: InvestigationState, runtime: Runtime):
        call = invocation(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        trusted = check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
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
