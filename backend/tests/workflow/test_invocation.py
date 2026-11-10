"""8D-3：调用上下文类型唯一，入口不会自动包装或接受调用方伪造的 claim。"""

import asyncio
from langgraph.checkpoint.memory import InMemorySaver

import pytest

from risk_platform.workflow.graph import build_investigation_graph
from risk_platform.workflow.invocation import InvestigationInvocation, require_invocation
from risk_platform.workflow.runner import InvestigationRunner
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
    runner = InvestigationRunner.__new__(InvestigationRunner)
    with pytest.raises(InvestigationAccessError):
        asyncio.run(runner.ainvoke(plan_id="P", context=values[kind]))


def test_public_invocation_is_explicit_and_has_no_recovery_mode():
    runtime = make_runtime(planning())
    invocation = InvestigationInvocation(runtime=runtime)
    assert require_invocation(invocation) is invocation
    assert invocation.execution is None and invocation.runtime is runtime


@pytest.mark.parametrize("mismatch", [None, "investigation", "round", "run"])
def test_claimed_context_has_one_runtime_and_rejects_mismatched_execution(mismatch):
    from dataclasses import replace
    from risk_platform.workflow.run_guard import RunExecutionContext, RunGuard
    from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, RecoveryPolicy

    runtime = make_runtime(planning())
    spec = RunIdentity(run_id="RUN", investigation_id="I", round_id="R", plan_id="P")
    guard = RunGuard(connection=object(), backend_pid=1, run_id="RUN", generation=1)
    execution = RunExecutionContext(identity=spec, policy=RecoveryPolicy(), guard=guard)
    if mismatch == "investigation":
        execution = replace(execution, identity=spec.model_copy(update={"investigation_id": "OTHER"}))
    elif mismatch == "round":
        execution = replace(execution, identity=spec.model_copy(update={"round_id": "OTHER"}))
    elif mismatch == "run":
        execution = replace(execution, guard=replace(guard, run_id="OTHER"))
    call = InvestigationInvocation(runtime=runtime, execution=execution)
    if mismatch:
        with pytest.raises(InvestigationAccessError):
            require_invocation(call, claimed=True)
    else:
        assert require_invocation(call, claimed=True) is call
        assert call.runtime is runtime
        assert not hasattr(call.execution, "context")
        assert not hasattr(call.execution, "planning_spec")
        assert not hasattr(call.execution, "planning_claim_id")


@pytest.mark.parametrize("field,value", [("scope", {}), ("index_profile", "baseline-v1"), ("max_top_k", 5),
    ("query_model", "model"), ("query_prompt_digest", "0" * 64),
    ("observation_model", "model"), ("observation_prompt_digest", "1" * 64)])
def test_run_identity_rejects_tool_and_phase_configuration(field, value):
    from pydantic import ValidationError
    from risk_platform.workflow.persistence.recovery_contracts import RunIdentity

    payload = dict(run_id="RUN", investigation_id="I", round_id="R", plan_id="P")
    with pytest.raises(ValidationError):
        RunIdentity.model_validate({**payload, field: value})
