"""8C 真实 PG ledger + 官方 PostgresSaver；模型/检索替身不产生外部费用。"""

import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.execution import knowledge
from risk_platform.execution.contracts import KnowledgeExecutionContext, KnowledgeSearchToolRequest
from risk_platform.knowledge.contracts import KnowledgeHit, KnowledgeSourceRef
from risk_platform.knowledge.context_resolution import ContextResolutionResult
from risk_platform.knowledge.retrieval import DenseChildRetrievalResult, RetrievedKnowledgeHit
from risk_platform.knowledge.serving_contracts import KnowledgeContext, RetrievalRouteSnapshot
from risk_platform.workflow import recovery
from risk_platform.agents.prompts import KNOWLEDGE_QUERY_SYSTEM_PROMPT
from risk_platform.workflow import access
from risk_platform.workflow.access import load_runtime_context
from risk_platform.workflow.checkpoints import JsonCheckpointSerializer, postgres_checkpointer
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, InvestigationTask, UserQuery
from risk_platform.workflow.observation import KnowledgeObservationDraft
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.business_inputs import BusinessOperationInput
from risk_platform.workflow.persistence.contracts import AcceptedPlan, InvestigationRecord, RoundRecord
from risk_platform.workflow.persistence.models import AttemptRow, ResultRow
from risk_platform.workflow.persistence.recovery_contracts import PhaseArtifact, RecoveryPolicy
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow, InvestigationRunSelectionRow
from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationAction
from test_ledger_postgres import database, pytestmark

pytest.importorskip("langgraph.checkpoint.postgres.aio")


class ProcessLost(BaseException):
    pass


class Agent:
    model = "test-model"

    def __init__(self):
        self.events = []
        self.query_failure = False

    def initial_plan(self, *_):
        pytest.fail("恢复不能重新规划或生成新 task_id")

    def formulate_knowledge_query(self, task, *_args, **_kwargs):
        self.events.append("query:" + task.task_id)
        if self.query_failure:
            raise ValueError("invalid model output")
        return KnowledgeSearchToolRequest(query="Query " + task.task_id, top_k=1)

    def observe_knowledge_round(self, plan, results):
        self.events.append("observation")
        return KnowledgeObservationDraft(execution_refs=tuple(row.execution_id for row in results), findings=tuple({
            "statement": "历史案例提供调查线索", "execution_refs": (row.execution_id,),
            "sources": ({"execution_id": row.execution_id, "context_id": row.payload.resolved.contexts[0].context_id,
                "source": row.payload.resolved.contexts[0].supporting_hits[0].source, "quote": "历史案例"},),
            "limitation": "仅为历史经验，当前案件尚未验证",
        } for row in results if row.payload and row.payload.resolved.contexts), knowledge_gaps=(),
            next_step_suggestions=(), narrative="历史经验不能直接确认为当前根因")


@asynccontextmanager
async def resources(url, agent, *, setup=False):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with postgres_checkpointer(url, setup=setup) as saver:
            runner = recovery.FirstRoundRecovery(agent, session_factory=sessions, checkpointer=saver)
            yield sessions, saver, runner
    finally:
        await engine.dispose()


async def seed(sessions, count=2, suffix=""):
    async with sessions.begin() as session:
        source = InitialPlanningInput(user_query=UserQuery(query="指标为何下降？", model_id="M"),
                                      available_capabilities=["KNOWLEDGE_RETRIEVAL"])
        await ledger.register_investigation(session, InvestigationRecord(investigation_id="I"+suffix, created_by="A", planning_input=source))
        await ledger.register_round(session, RoundRecord(investigation_id="I"+suffix, round_id="R"+suffix, round_number=1))
        await ledger.accept_plan(session, AcceptedPlan(investigation_id="I"+suffix, round_id="R"+suffix, plan_id="P"+suffix, plan_version=1,
            plan=InvestigationPlan(goal="调查历史背景", tasks=[InvestigationTask(task_id=f"T{i}", objective="查历史",
                capability="KNOWLEDGE_RETRIEVAL", expected_result="线索") for i in range(count)])))


async def context(sessions, actor="A", permission=lambda *_: True, timeout=30, suffix=""):
    async with sessions() as session:
        return await load_runtime_context(session, investigation_id="I"+suffix, round_id="R"+suffix,
            actor_id=actor, timeout_seconds=timeout,
            tools=(KnowledgeSearchTool(KnowledgeExecutionContext(actor_id=actor, index_profile="parent-child-v1", scope={"model_id": "M"}, timeout_seconds=timeout), lambda *_: True),),
            authorize_resource=permission,
            action=InvestigationAction.READ if actor == "reader" else InvestigationAction.CONTINUE)


def retrieval(monkeypatch, agent):
    route = {"collection": "physical-v1"}
    async def search(request, **_):
        task = request.query.split()[-1]
        agent.events.append("search:" + task)
        hit = KnowledgeHit(source=KnowledgeSourceRef(document_id="D-"+task, document_version="v1", chunk_id="C-"+task),
            document_type="INCIDENT_REPORT", model_id="M", title="历史报告", section_path="原因", content="历史案例", rank=1)
        return DenseChildRetrievalResult(snapshot=RetrievalRouteSnapshot(index_profile="parent-child-v1",
            collection=route["collection"], route_version=1), hits=(RetrievedKnowledgeHit(hit=hit, score=.8),))
    async def resolve(result, **_):
        return ContextResolutionResult(snapshot=result.snapshot, contexts=tuple(KnowledgeContext(context_id="P-"+row.hit.source.chunk_id,
            content="历史案例说明需要核查当前数据", supporting_hits=(row.hit,)) for row in result.hits))
    monkeypatch.setattr(knowledge, "retrieve_dense_children", search)
    monkeypatch.setattr(knowledge, "resolve_knowledge_contexts", resolve)
    return route


def test_real_checkpointer_reopen_reuses_all_results_and_original_snapshots(database, monkeypatch):
    agent = Agent()
    route = retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert first["round_complete"] and first["investigation_status"] == "OPEN"
            assert [row.task_id for row in first["task_execution_results"]] == ["T0", "T1"]
            assert first["observation"].status == "SUCCEEDED"
        count = list(agent.events)
        route["collection"] = "physical-v2"
        # 独立新 engine / saver / compiled graph，读取相同 PG checkpoint。
        async with resources(database, agent) as (sessions, saver, runner):
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B", timeout=60)))
            assert second == first and agent.events == count
            async with sessions() as session:
                assert len(list(await session.scalars(select(ResultRow)))) == 5
                assert all(row.actor_id == "A" for row in await session.scalars(select(AttemptRow)))
    asyncio.run(scenario())


def test_persisted_first_run_uses_shared_loop_and_resume_reuses_results(database, monkeypatch):
    from risk_platform.workflow import graph as graph_module
    from risk_platform.workflow import nodes
    agent = Agent()
    retrieval(monkeypatch, agent)
    calls = []
    async def shared(*args, **kwargs):
        assert kwargs["execute_phase"] is not None
        calls.append("shared-loop")
        return await nodes.task_execution(*args, **kwargs)
    monkeypatch.setattr(graph_module, "task_execution", shared)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            events = list(agent.events)
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert first == second and agent.events == events and calls == ["shared-loop"]
            async with sessions() as session:
                attempts = (await session.scalars(select(AttemptRow))).all()
                assert len(attempts) == 5 and all(row.status == "COMMITTED" for row in attempts)
    asyncio.run(scenario())


def test_fresh_and_interrupted_resume_use_same_explicit_invocation_contract(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    original, interrupted = recovery._phase, {"done": False}
    async def interrupt(*args):
        artifact = await original(*args)
        if not interrupted["done"]:
            interrupted["done"] = True
            raise ProcessLost("已提交 Query，检查点节点尚未完成")
        return artifact
    monkeypatch.setattr(recovery, "_phase", interrupt)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            public = InvestigationInvocation(runtime=await context(sessions))
            calls = []
            invoke = runner._graph.ainvoke
            async def capture(initial, *args, context, **kwargs):
                assert isinstance(context, InvestigationInvocation)
                assert context.recovery is not None and context.runtime is public.runtime
                calls.append(initial)
                return await invoke(initial, *args, context=context, **kwargs)
            monkeypatch.setattr(runner._graph, "ainvoke", capture)
            with pytest.raises(ProcessLost):
                await runner.ainvoke(plan_id="P", context=public)
            output = await runner.ainvoke(plan_id="P", context=public)
            assert calls[0]["checkpoint_schema_version"] == 2 and calls[1] is None
            assert public.recovery is None  # 内部 claim 不回填到调用方上下文。
            assert output["round_complete"] and agent.events.count("query:T0") == 1
    asyncio.run(scenario())


def test_invalid_candidate_is_committed_as_query_failure_without_search(database, monkeypatch):
    agent = Agent()
    def invalid(*_, **__):
        agent.events.append("invalid-query")
        return {"query": "q", "actor_id": "forged"}
    agent.formulate_knowledge_query = invalid
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            result = first["task_execution_results"][0]
            assert result.status == "FAILED" and result.error.code == "QUERY_FORMULATION_INVALID"
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert second == first and agent.events == ["invalid-query"]
            async with sessions() as session:
                assert len((await session.scalars(select(ResultRow))).all()) == 2
    asyncio.run(scenario())


def test_persisted_execution_requires_accepted_plan_before_any_phase(database):
    agent = Agent()
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            with pytest.raises(ledger.LedgerIntegrityError, match="已接受计划"):
                await runner.ainvoke(plan_id="UNACCEPTED", context=InvestigationInvocation(runtime=await context(sessions)))
            assert agent.events == []
            async with sessions() as session:
                assert (await session.scalars(select(AttemptRow))).all() == []
    asyncio.run(scenario())


@pytest.mark.parametrize("old_node", ["load_plan", "finish_round"])
def test_old_topology_pending_node_is_rejected_before_external_call(database, monkeypatch, old_node):
    agent = Agent()
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            async def old_checkpoint(*_):
                return SimpleNamespace(values={}, next=(old_node,))
            monkeypatch.setattr(runner._graph, "aget_state", old_checkpoint)
            with pytest.raises(ledger.LedgerIntegrityError, match="旧拓扑"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert agent.events == []
            async with sessions() as session:
                assert (await session.scalars(select(AttemptRow))).all() == []
    asyncio.run(scenario())


def test_recovery_uses_shared_replanning_and_topology(database, monkeypatch):
    from risk_platform.workflow import graph as graph_module
    agent = Agent()
    retrieval(monkeypatch, agent)
    calls = []
    original = graph_module.replanning
    def shared_replanning(state):
        calls.append(state["observation"])
        return original(state)
    monkeypatch.setattr(graph_module, "replanning", shared_replanning)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            assert set(runner._graph.get_graph().nodes) == {
                "__start__", "prepare_plan", "task_execution", "observation", "replanning", "__end__",
            }
            result = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert result["round_complete"] and result["investigation_status"] == "OPEN"
            assert len(calls) == 1 and calls[0]["status"] == "SUCCEEDED"
    asyncio.run(scenario())


def test_old_checkpoint_version_is_rejected_without_reexecuting(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            events = list(agent.events)
            async with sessions() as session:
                run = (await session.scalars(select(InvestigationRunRow))).one()
            await runner._graph.aupdate_state({"configurable": {"thread_id": run.run_id}},
                                             {"checkpoint_schema_version": 1})
            with pytest.raises(ledger.LedgerIntegrityError, match="版本不兼容"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert agent.events == events
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["QUERY_FORMULATION", "KNOWLEDGE_EXECUTION", "OBSERVATION"])
def test_crash_after_phase_commit_before_node_checkpoint_reuses_exact_artifact(database, monkeypatch, stage):
    agent = Agent()
    retrieval(monkeypatch, agent)
    original, crashed = recovery._phase, {"done": False}
    async def crash(*args):
        value = await original(*args)
        operation = args[2]
        if operation.operation_type == stage and not crashed["done"]:
            crashed["done"] = True
            raise ProcessLost("post-commit process lost")
        return value
    monkeypatch.setattr(recovery, "_phase", crash)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            with pytest.raises(ProcessLost):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
        async with resources(database, agent) as (sessions, saver, runner):
            output = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert output["observation"].status == "SUCCEEDED"
            assert agent.events.count("query:T0") == agent.events.count("query:T1") == 1
            assert agent.events.count("search:T0") == agent.events.count("search:T1") == 1
            assert agent.events.count("observation") == 1
    asyncio.run(scenario())


def test_query_failure_is_committed_reused_and_never_becomes_empty_success(database, monkeypatch):
    agent = Agent()
    agent.query_failure = True
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert first["task_execution_results"][0].status == "FAILED"
            assert first["task_execution_results"][0].error.code == "QUERY_FORMULATION_INVALID"
            assert first["observation"].status == "INCONCLUSIVE"
        async with resources(database, agent) as (sessions, saver, runner):
            second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            assert second == first and agent.events == ["query:T0"]
    asyncio.run(scenario())


def test_t0_reused_t1_uncommitted_becomes_uncertain_and_requires_explicit_policy(database, monkeypatch):
    agent = Agent()
    route = retrieval(monkeypatch, agent)
    original, lost = ledger.commit_result, {"done": False}
    async def commit(session, attempt_id, result_id, artifact):
        result = await original(session, attempt_id, result_id, artifact)
        if artifact.execution and artifact.execution.task_id == "T1" and not lost["done"]:
            lost["done"] = True
            # 外部已返回、SQL flush 已发生，但事务整体未提交即失去进程。
            raise ProcessLost("uncommitted external completion")
        return result
    monkeypatch.setattr(ledger, "commit_result", commit)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            with pytest.raises(ProcessLost):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
        before = list(agent.events)
        async with resources(database, agent) as (sessions, saver, runner):
            b = await context(sessions, "B")
            with pytest.raises(recovery.RecoveryBlockedError, match="不确定"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=b))
            assert agent.events == before
            async with sessions() as session:
                uncertain = list(await session.scalars(select(AttemptRow).where(AttemptRow.status == "UNCERTAIN")))
                assert len(uncertain) == 1 and uncertain[0].actor_id == "A"
            route["collection"] = "physical-v2"
            output = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=b), policy=RecoveryPolicy(retry_uncertain=True))
            assert [row.payload.retrieved.snapshot.collection for row in output["task_execution_results"]] == ["physical-v1", "physical-v2"]
            assert agent.events.count("query:T0") == agent.events.count("query:T1") == agent.events.count("search:T0") == 1
            assert agent.events.count("search:T1") == 2
            async with sessions() as session:
                attempts = list(await session.scalars(select(AttemptRow).where(AttemptRow.status == "COMMITTED")))
                assert any(row.actor_id == "B" for row in attempts)
    asyncio.run(scenario())


@pytest.mark.parametrize("permission", ["read", "continue", "revoked"])
def test_resume_checks_both_permissions_before_reading_checkpoint(database, monkeypatch, permission):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
            valid = await context(sessions)
            def policy(_, action, __):
                return action == "investigation:" + permission
            denied = replace(valid, authorize_resource=policy)
            async def forbidden(*_):
                pytest.fail("权限不足不应先读取 checkpoint")
            monkeypatch.setattr(runner._graph, "aget_state", forbidden)
            with pytest.raises(InvestigationAccessError):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=denied))
            assert not agent.events
    asyncio.run(scenario())


def test_duplicate_active_resume_is_rejected_without_second_external_call(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    base_search = knowledge.retrieve_dense_children
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def search(*args, **kwargs):
            entered.set()
            await release.wait()
            return await base_search(*args, **kwargs)
        monkeypatch.setattr(knowledge, "retrieve_dense_children", search)
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            a, b = await context(sessions), await context(sessions, "B")
            first = asyncio.create_task(runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a)))
            try:
                await asyncio.wait_for(entered.wait(), 15)
                with pytest.raises(recovery.RecoveryBlockedError, match="正在运行"):
                    await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=b))
            finally:
                release.set()
                await asyncio.wait_for(first, 20)
            assert agent.events.count("query:T0") == agent.events.count("search:T0") == 1
    asyncio.run(scenario())


def test_checkpoint_result_corruption_fails_before_any_new_external_call(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            a = await context(sessions)
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a))
            async with sessions() as session:
                run = (await session.scalars(select(InvestigationRunRow))).one()
            forged = first["task_execution_results"][0].model_copy(update={"execution_id": "forged-exec"})
            await runner._graph.aupdate_state({"configurable": {"thread_id": run.run_id}},
                {"task_execution_results": [forged.model_dump(mode="json")]}, as_node="task_execution")
            before = list(agent.events)
            with pytest.raises(ledger.LedgerIntegrityError, match="执行事实"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a))
            assert before == agent.events
    asyncio.run(scenario())


def test_lost_lock_connection_fences_old_worker_after_new_generation_finishes(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    base_search, claim_original, guards = knowledge.retrieve_dense_children, recovery._claim, []
    @asynccontextmanager
    async def capture(*args, **kwargs):
        async with claim_original(*args, **kwargs) as invocation:
            guards.append(invocation.guard)
            yield invocation
    monkeypatch.setattr(recovery, "_claim", capture)
    async def scenario():
        entered, release, count = asyncio.Event(), asyncio.Event(), {"value": 0}
        async def search(*args, **kwargs):
            count["value"] += 1
            if count["value"] == 1:
                entered.set()
                await release.wait()
            return await base_search(*args, **kwargs)
        monkeypatch.setattr(knowledge, "retrieve_dense_children", search)
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            a, b = await context(sessions), await context(sessions, "B")
            old = asyncio.create_task(runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a)))
            try:
                await asyncio.wait_for(entered.wait(), 15)
                # 只终止本测试刚创建的专用锁连接，模拟旧进程失联；不触及其他会话。
                async with sessions.begin() as session:
                    assert await session.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": guards[0].backend_pid})
                new = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=b), policy=RecoveryPolicy(retry_uncertain=True))
                assert new["round_complete"]
            finally:
                release.set()
            with pytest.raises(recovery.RecoveryFencedError):
                await old
            async with sessions() as session:
                rows = list(await session.scalars(select(AttemptRow).where(AttemptRow.status == "UNCERTAIN")))
                assert len(rows) == 1 and rows[0].actor_id == "A"
                assert len(list(await session.scalars(select(ResultRow)))) == 3
    asyncio.run(scenario())


def test_multiple_committed_results_require_explicit_immutable_selection(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            a = await context(sessions)
            value = BusinessOperationInput(investigation_id="I", round_id="R", plan_id="P", task_id="T0",
                operation_type="QUERY_FORMULATION", model_name=agent.model,
                prompt_version=hashlib.sha256(KNOWLEDGE_QUERY_SYSTEM_PROMPT.encode()).hexdigest(), max_top_k=20)
            async with sessions.begin() as session:
                key = await access.register_authorized_operation(session, value, context=a)
                for index in (1, 2):
                    await access.start_authorized_attempt(session, key, f"legacy-query-{index}", context=a, retry=index > 1)
                    await ledger.commit_result(session, f"legacy-query-{index}", f"legacy-result-{index}",
                        PhaseArtifact(query_request=KnowledgeSearchToolRequest(query="Query T0", top_k=1)))
            with pytest.raises(recovery.RecoveryBlockedError, match="多份已提交"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a))
            assert not agent.events
            output = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a),
                policy=RecoveryPolicy(selected_attempts={key: "legacy-query-1"}))
            assert output["round_complete"] and "query:T0" not in agent.events
            with pytest.raises(ledger.LedgerConflictError, match="重新选择"):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a),
                    policy=RecoveryPolicy(selected_attempts={key: "legacy-query-2"}))
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["FAILED", "REJECTED", "NO_HITS"])
def test_committed_failure_rejection_and_zero_hits_never_implicitly_rerun(database, monkeypatch, status):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def unavailable(*args, **kwargs):
        from risk_platform.knowledge.retrieval import ChildRetrievalIntegrityError
        agent.events.append("search:failed")
        raise ChildRetrievalIntegrityError("source mismatch")
    async def empty(request, **kwargs):
        agent.events.append("search:empty")
        return DenseChildRetrievalResult(snapshot=RetrievalRouteSnapshot(index_profile="parent-child-v1",
            collection="empty-physical", route_version=1), hits=())
    if status == "FAILED":
        monkeypatch.setattr(knowledge, "retrieve_dense_children", unavailable)
    elif status == "NO_HITS":
        monkeypatch.setattr(knowledge, "retrieve_dense_children", empty)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions, 1)
            a = await context(sessions)
            if status == "REJECTED":
                a = replace(a, tools=(replace(a.tools[0], authorize=lambda *_: False),))
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=a))
            assert first["task_execution_results"][0].status == ("SUCCEEDED" if status == "NO_HITS" else status)
            assert first["observation"].status == "INCONCLUSIVE"
        events = list(agent.events)
        async with resources(database, agent) as (sessions, saver, runner):
            output = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert output == first and events == agent.events
    asyncio.run(scenario())


def test_lock_connections_do_not_starve_single_connection_ledger_pool(database, monkeypatch):
    agent = Agent()
    retrieval(monkeypatch, agent)
    async def scenario():
        engine = create_async_engine(database, pool_size=1, max_overflow=0, pool_timeout=3)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed(sessions, 1, suffix="A")
            await seed(sessions, 1, suffix="B")
            async with postgres_checkpointer(database, setup=True) as saver:
                runner = recovery.FirstRoundRecovery(agent, session_factory=sessions, checkpointer=saver)
                a, b = await context(sessions, suffix="A"), await context(sessions, "B", suffix="B")
                results = await asyncio.wait_for(asyncio.gather(runner.ainvoke(plan_id="PA", context=InvestigationInvocation(runtime=a)),
                    runner.ainvoke(plan_id="PB", context=InvestigationInvocation(runtime=b))), 30)
                assert all(result["round_complete"] for result in results)
        finally:
            await engine.dispose()
    asyncio.run(scenario())


def test_actual_process_exit_after_t0_commit_resumes_in_new_process_state(database, monkeypatch):
    agent = Agent()
    route = retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed(sessions)
        child = r'''
import asyncio, os, pytest
from test_recovery_postgres import Agent, resources, context, retrieval
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow import recovery
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
agent = Agent()
retrieval(pytest.MonkeyPatch(), agent)
original = recovery._phase
async def crash(*args):
    value = await original(*args)
    if args[2].operation_type == "KNOWLEDGE_EXECUTION" and args[2].task_id == "T0":
        os._exit(23)
    return value
recovery._phase = crash
async def main():
    async with resources(os.environ["RECOVERY_TEST_DATABASE_URL"], agent) as (sessions, saver, runner):
        await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
asyncio.run(main())
'''
        environment = dict(os.environ)
        environment["RECOVERY_TEST_DATABASE_URL"] = database.render_as_string(hide_password=False)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parent) + os.pathsep + environment.get("PYTHONPATH", "")
        # 只终止子进程本身；真实 TCP/PG 会话由操作系统关闭，不执行 Python finally。
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", child], env=environment,
                                          capture_output=True, timeout=35)
        assert result.returncode == 23, result.stderr.decode(errors="replace")[-2000:]
        route["collection"] = "physical-v2"
        async with resources(database, agent) as (sessions, saver, runner):
            output = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions, "B")))
            assert agent.events == ["query:T1", "search:T1", "observation"]
            assert [row.payload.retrieved.snapshot.collection for row in output["task_execution_results"]] == ["physical-v1", "physical-v2"]
    asyncio.run(scenario())
