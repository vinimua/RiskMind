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
    session_factory, checkpointer,
):
    """持久化 runner 内部使用的唯一拓扑，不提供无账本执行入口。"""
    if session_factory is None or checkpointer is None:
        raise ValueError("正式执行必须配置账本会话与检查点存储")

    def require_run_context(runtime):
        # 声明类型与实际调用一致，不再兼容裸 InvestigationRuntimeContext。
        return require_invocation(runtime.context, claimed=True)

    # 它把两项依赖交给账本适配器：
    # session_factory：访问账本数据库；
    # call.execution：本轮运行身份、运行锁、代次和重试策略。
    def phase_executor(call):
        return phase_execution.ledger_phase_executor(session_factory, call.execution, runtime=call.runtime, agent=investigation_agent)

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
        call = require_run_context(runtime)
        return await planning.prepare_plan(state, call.execution, sessions=session_factory, agent=investigation_agent, runtime=call.runtime)

    async def execute_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = require_run_context(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        #进入任务执行前，核对“当前身份是否允许继续调查，以及 State 中的调查输入是否与可信案件一致”
        check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
        update = await task_execution(state, investigation_agent=investigation_agent, runtime_context=call.runtime,
                                    execute_phase=phase_executor(call))
        return to_checkpoint_data(update)

    async def observe_node(state: InvestigationState, runtime: Runtime[InvestigationInvocation]):
        call = require_run_context(runtime)
        source = InitialPlanningInput.model_validate(state["planning_input"])
        check_runtime(call.runtime, InvestigationAction.CONTINUE, source)
        update = await observation(state, investigation_agent=investigation_agent, runtime_context=call.runtime,
                                 execute_phase=phase_executor(call))
        return to_checkpoint_data(update)

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
