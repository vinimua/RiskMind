"""8D-3：调用上下文类型唯一，入口不会自动包装或接受调用方伪造的 claim。"""

import asyncio
from langgraph.checkpoint.memory import InMemorySaver

import pytest

from risk_platform.workflow.graph import build_investigation_graph
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation
from risk_platform.workflow.recovery import FirstRoundRecovery
from risk_platform.workflow.runtime import InvestigationAccessError
from test_runtime import make_runtime, planning


@pytest.mark.parametrize("kind", ["bare", "missing", "dict", "invalid-runtime", "forged-claim"])
def test_graph_rejects_invalid_context_before_planning(kind):
    source = planning()
    trusted = make_runtime(source)
    values = {
        "bare": trusted,
        "missing": None,
        "dict": {"runtime": {"actor_id": "forged"}},
        "invalid-runtime": InvestigationInvocation(runtime=None),
        "forged-claim": InvestigationInvocation(runtime=trusted, execution=object()),
    }
    class Agent:
        def initial_plan(self, *_):
            pytest.fail("非法调用上下文不能进入规划模型")
    with pytest.raises(InvestigationAccessError):
        asyncio.run(build_investigation_graph(Agent(), session_factory=object(), checkpointer=InMemorySaver()).ainvoke(
            {"planning_input": source}, config={"configurable": {"thread_id": "invalid-context"}},
            context=values[kind]))


@pytest.mark.parametrize("kind", ["bare", "missing", "dict", "forged-claim"])
def test_persistent_entry_rejects_invalid_context_before_database_or_checkpoint(kind):
    trusted = make_runtime(planning())
    values = {"bare": trusted, "missing": None, "dict": {"runtime": trusted},
              "forged-claim": InvestigationInvocation(runtime=trusted, execution=object())}
    # 未设置任何数据库/检查点依赖；非法输入必须在尝试访问依赖前拒绝。
    runner = FirstRoundRecovery.__new__(FirstRoundRecovery)
    with pytest.raises(InvestigationAccessError):
        asyncio.run(runner.ainvoke(plan_id="P", context=values[kind]))


def test_public_invocation_is_explicit_and_has_no_recovery_mode():
    runtime = make_runtime(planning())
    invocation = InvestigationInvocation(runtime=runtime)
    assert require_invocation(invocation) is invocation
    assert invocation.execution is None and invocation.runtime is runtime
