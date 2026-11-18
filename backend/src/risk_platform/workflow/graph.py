from functools import partial
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.workflow import planning, phase_execution
from risk_platform.workflow.checkpoints import to_checkpoint_data
from risk_platform.workflow.contracts import InitialPlanningInput
from risk_platform.workflow.runtime import InvestigationAction, check_runtime
from risk_platform.workflow.nodes import (
    observation,
    task_execution,
)
from risk_platform.workflow.state import InvestigationState
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation


def _route_after_observation(
    state: InvestigationState,
) -> Literal["continue", "round_complete"]:

    # END 只是结束本次图调用，不代表关闭案件。
    return "round_complete" if state.get("round_complete", False) else "continue"


def build_investigation_graph(
    investigation_agent: InvestigationAgent, *,
    session_factory, checkpointer,
):
    """持久化 runner 内部使用的唯一拓扑，不提供无账本执行入口。"""
    if session_factory is None or checkpointer is None:
        raise ValueError("正式执行必须配置账本会话与检查点存储")

    # LangGraph 传入 runtime
    #     ↓
    # 取出 runtime.context
    #     ↓
    # 检查调用包装与内部执行信息
    #     ↓
    # 返回给变量 call
# call.runtime     # 身份、案件、工具绑定
# call.execution   # 运行规格、guard、重试策略
    async def plan_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = await planning.current_round_call(state, require_invocation(runtime.context, claimed=True), sessions=session_factory)
        return await planning.prepare_plan(state, call.execution, sessions=session_factory, agent=investigation_agent, runtime=call.runtime)

    async def execute_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = await planning.current_round_call(state, require_invocation(runtime.context, claimed=True), sessions=session_factory)
        update = await task_execution(state, investigation_agent=investigation_agent, runtime_context=call.runtime,
                                    query_phase=partial(phase_execution.query_phase, sessions=session_factory, call=call,
                                                        model_name=investigation_agent.model),
                                    tool_phase=partial(phase_execution.tool_phase, sessions=session_factory, call=call))
        return to_checkpoint_data(update)

# graph.py：传入函数并预填依赖
#     ↓
# nodes.observation()：调用它
#     ↓
# phase_execution.observation_phase()：执行或复用解释
#     ↓
# _phase()：负责账本和事务
    async def observe_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        root = require_invocation(runtime.context, claimed=True)
        # root 是校验后的 runtime.context，不是外层的 LangGraph runtime
    # runtime                         LangGraph 运行容器
    # └─ context                  InvestigationInvocation
    #      ├─ runtime             可信业务上下文
    #      └─ execution           运行身份、Guard、恢复策略
        call = await planning.current_round_call(state, root, sessions=session_factory)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
        async with session_factory() as session:
            material = await planning.round_material(session, call.execution, call.runtime)
        update = await observation(state, investigation_agent=investigation_agent, runtime_context=call.runtime,
                                 observation_phase=partial(phase_execution.observation_phase, sessions=session_factory,
                                                           call=call, model_name=investigation_agent.model),
                                 previous_observations=material.get("previous_observations", ()))
        update.update(await planning.advance_round(state | update, call, root, sessions=session_factory))
        return to_checkpoint_data(update)

    graph = StateGraph(InvestigationState, context_schema=InvestigationInvocation)
    graph.add_node("prepare_plan", plan_node)
    graph.add_node("task_execution", execute_node)
    graph.add_node("observation", observe_node)

    graph.add_edge(START, "prepare_plan")
    graph.add_edge("prepare_plan", "task_execution")
    graph.add_edge("task_execution", "observation")
    graph.add_conditional_edges(
        "observation",
        _route_after_observation,
        {
            "continue": "prepare_plan",
            "round_complete": END,
        },
    )

    return graph.compile(checkpointer=checkpointer)
