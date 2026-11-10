"""只供业务单元测试的阶段替身，不是生产调查入口。"""

from risk_platform.workflow import nodes
from risk_platform.workflow.runtime import InvestigationAction, check_runtime
from risk_platform.execution.tools import ToolCall


async def run_phase(kind, produce, **_):
    return await produce()


async def run_business_nodes(agent, source, runtime):
    """直接测试业务函数组合，不构造无账本生产图。"""
    check_runtime(runtime, InvestigationAction.CONTINUE, source)
    state = {"planning_input": source}
    state.update(nodes.initial_planning(state, investigation_agent=agent))
    state.update(await nodes.task_execution(state, investigation_agent=agent, runtime_context=runtime,
        execute_phase=run_phase))
    check_runtime(runtime, InvestigationAction.CONTINUE, source)
    state.update(await nodes.observation(state, investigation_agent=agent, runtime_context=runtime,
                                        execute_phase=run_phase))
    state.update(nodes.replanning(state))
    return state


def tool_call(arguments):
    return ToolCall("knowledge_search", arguments)


def runtime_for(source, execution, *, authorize=lambda *_: True):
    from test_runtime import make_runtime
    return make_runtime(source, execution, authorize_knowledge=authorize, allow_unscoped=True)
