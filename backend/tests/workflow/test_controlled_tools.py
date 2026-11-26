"""真实 PostgreSQL/检查点验收；两个不同的操作共用审批和恢复路径。"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from risk_platform.execution import tools
from risk_platform.execution.contracts import TaskExecutionResult
from risk_platform.workflow.invocation import InvestigationInvocation
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import resources, seed, context, ProcessLost


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target: str
    value: str


class Ref(BaseModel):
    reference_id: str


def install(monkeypatch, events, remote, name):
    from risk_platform.execution.actions import ApprovalPolicy
    async def prepare(sessions, runtime, request):
        return Args(target=request.target)
    async def precondition(sessions, runtime, material):
        return remote.get("allowed", True)
    async def authorize_material(sessions, runtime, material):
        return True
    def result(task, material, action_id):
        now = datetime.now(timezone.utc)
        return TaskExecutionResult(execution_id="exec-" + action_id, task_id=task.task_id,
            capability=task.capability, tool_name=name, result_version="1", status="SUCCEEDED",
            started_at=now, completed_at=now, payload={"target": material.target, "value": name})
    async def execute(config: RunnableConfig, **arguments):
        cfg = config["configurable"]
        events.append("execute")
        remote[cfg["action_id"]] = result(cfg["task"], cfg["approved_material"], cfg["action_id"])
        if remote.pop("crash", False):
            raise ProcessLost()
        return remote[cfg["action_id"]]
    async def verify(runtime, material, action_id):
        events.append("verify")
        if remote.get("unknown"):
            return "UNKNOWN"
        return Receipt.model_validate(remote[action_id].payload) if action_id in remote else "NOT_APPLIED"
    definition = tools.ToolDefinition(
        tool=StructuredTool.from_function(coroutine=execute, name=name, description="Controlled test operation",
            args_schema=Args, infer_schema=False), capability="KNOWLEDGE_RETRIEVAL",
        execution_limits=lambda _: {"target": "test"}, validate_request=lambda *_: None,
        result_model=Receipt, to_observation_items=lambda p: [{"reference_id": "receipt", "content": p.model_dump()}],
        reference_model=Ref, validate_reference=lambda *_: None, authorize_result=lambda *_: True,
        validate_result=lambda req, limits, payload: None,
        approval=ApprovalPolicy(ttl_seconds=300, material_model=Args, prepare=prepare,
            authorize_material=authorize_material, precondition=precondition, verify=verify))
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType(dict(tools.TOOL_DEFINITIONS) | {name: definition}))
    return definition


class Agent:
    model = "test-only"
    def __init__(self, name, events):
        self.name, self.events = name, events
    def formulate_tool_call(self, task, *_, **__):
        self.events.append("query")
        return tools.ToolCall(self.name, {"target": "test"})
    def observe_round(self, plan, results, **kwargs):
        from risk_platform.workflow.observation import ObservationDraft
        self.events.append("observe")
        return ObservationDraft(findings=[], unresolved_questions=["No root cause evidence"],
            action="STOP", stop_reason="Controlled action finished")


async def runtime(sessions, name, permissions):
    from types import SimpleNamespace
    value = await context(sessions, permission=lambda actor, action, resource: permissions.get(action, True))
    return replace(value, tools=(SimpleNamespace(name=name, validate_binding=lambda *_: None),))


@pytest.mark.parametrize("name", ["test_send", "test_update"])
def test_approval_pauses_releases_lock_and_resumes_exactly_once(database, monkeypatch, name):
    from risk_platform.execution import actions
    events, remote, permissions = [], {}, {}
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            first = await runner.ainvoke(plan_id="P", context=call)
            assert first["status"] == "WAITING_APPROVAL" and events == ["query"]
            action_id = first["pending_action"]["action_id"]
            # A second graph invocation can acquire the lock, but cannot bypass approval.
            again = await runner.ainvoke(plan_id="P", context=call)
            assert again["pending_action"]["action_id"] == action_id and events == ["query"]
            view = await actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                digest=first["pending_action"]["digest"], approved=True)
            assert view["status"] == "APPROVED"
            await actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                digest=view["digest"], approved=True)
            done = await runner.ainvoke(plan_id="P", context=call)
            assert done["round_complete"] and events.count("execute") == 1
            repeated = await runner.ainvoke(plan_id="P", context=call)
            assert repeated == done and events.count("execute") == 1 and events.count("query") == 1
            saved = await actions.inspect(sessions, action_id, runtime=call.runtime)
            assert saved["status"] == "SUCCEEDED" and saved["audit"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["reject", "expire", "expire_waiting", "invalidate", "crash", "uncertain", "concurrent"])
def test_controlled_action_failure_and_recovery(database, monkeypatch, mode):
    from risk_platform.execution import actions
    events, remote, permissions = [], {}, {}
    name = "test_update"
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            pending = (await runner.ainvoke(plan_id="P", context=call))["pending_action"]
            action_id, digest = pending["action_id"], pending["digest"]
            with pytest.raises(ValueError):
                await actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                    digest="0" * 64, approved=True)
            permissions["action:approve"] = False
            with pytest.raises(PermissionError):
                await actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                    digest=digest, approved=True)
            permissions["action:approve"] = True
            if mode in {"expire", "expire_waiting"}:
                from datetime import timedelta
                future = datetime.fromisoformat(pending["expires_at"]) + timedelta(seconds=1)
                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return future
                monkeypatch.setattr(actions, "datetime", Clock)
            decisions = [actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                digest=digest, approved=mode != "reject") for _ in range(0 if mode == "expire_waiting" else 2 if mode == "concurrent" else 1)]
            await asyncio.gather(*decisions)
            if mode == "invalidate":
                remote["allowed"] = False
            if mode in {"crash", "uncertain"}:
                remote["crash"] = True
                with pytest.raises(ProcessLost):
                    await runner.ainvoke(plan_id="P", context=call)
            if mode == "uncertain":
                remote["unknown"] = True
                waiting = await runner.ainvoke(plan_id="P", context=call)
                assert waiting["status"] == "WAITING_VERIFICATION" and events.count("execute") == 1
                snapshot = await runner._graph.aget_state({"configurable": {"thread_id": waiting["run_id"]}})
                assert snapshot.interrupts
                remote["unknown"] = False
            if mode == "concurrent":
                outputs = await asyncio.gather(runner.ainvoke(plan_id="P", context=call),
                    runner.ainvoke(plan_id="P", context=call), return_exceptions=True)
                assert sum(isinstance(o, dict) for o in outputs) >= 1
            done = await runner.ainvoke(plan_id="P", context=call)
            assert done["round_complete"]
            result = done["task_execution_results"][0]
            if mode in {"reject", "expire", "expire_waiting", "invalidate"}:
                assert result.status == "REJECTED" and "execute" not in events
            else:
                assert result.status == "SUCCEEDED" and events.count("execute") == 1
                if mode in {"crash", "uncertain"}:
                    assert "verify" in events
                    saved = await actions.inspect(sessions, action_id, runtime=call.runtime)
                    assert any(event["event"] == "VERIFIED" for event in saved["audit"])
    asyncio.run(scenario())


def test_http_confirmation_uses_saved_material_and_same_runner(database, monkeypatch):
    import httpx
    from risk_platform.entrypoints import EntryServices
    from risk_platform.main import create_app
    events, remote, permissions = [], {}, {}
    name = "test_send"
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            trusted = await runtime(sessions, name, permissions)
            pending = (await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=trusted)))["pending_action"]
            services = EntryServices(sessions=sessions, runner=runner, chat_agent=None,
                tools_for=lambda *_: trusted.tools, authorize_resource=trusted.authorize_resource,
                authorize_conversation=lambda *_: True)
            app = create_app(entry_services=services, authenticate=lambda _: "A")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                url = "/actions/" + pending["action_id"]
                read = await client.get(url)
                assert read.status_code == 200 and read.json()["material"] == {"target": "test"}
                wrong = await client.post(url + "/confirmation", json={"digest": pending["digest"], "approved": True, "target": "forged"})
                assert wrong.status_code == 422 and not remote
                confirmed = await client.post(url + "/confirmation", json={"digest": pending["digest"], "approved": True})
                assert confirmed.status_code == 200 and confirmed.json()["round_complete"]
                assert events.count("execute") == 1
                permissions["action:read"] = False
                assert (await client.get(url)).status_code == 403
    asyncio.run(scenario())


def test_report_export_uses_committed_observation_and_download_authorization(database, monkeypatch, tmp_path):
    import httpx
    from risk_platform.entrypoints import EntryServices
    from risk_platform.main import create_app
    from risk_platform.execution.report_export import ReportExportTool
    from risk_platform.workflow.contracts import InitialPlanningInput, UserQuery, InvestigationTask, InvestigationPlan
    from risk_platform.workflow.persistence import repository as ledger
    from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord, AcceptedPlan
    from test_recovery_postgres import Agent as KnowledgeAgent, retrieval
    class ReportAgent(KnowledgeAgent):
        def initial_plan(self, source, **kwargs):
            return InvestigationPlan(goal="Export committed findings", tasks=[InvestigationTask(
                task_id="EXPORT", objective="Export investigation report", expected_result="Markdown report", capability="COMPUTATION")])
        def formulate_tool_call(self, task, *args, **kwargs):
            if task.task_id == "EXPORT":
                self.events.append("query:EXPORT")
                return tools.ToolCall("export_investigation_report", {"investigation_id": "I"})
            return super().formulate_tool_call(task, *args, **kwargs)
        def observe_round(self, plan, results, **kwargs):
            from risk_platform.workflow.observation import ObservationDraft
            if results[0].task_id == "EXPORT":
                return ObservationDraft(findings=[], unresolved_questions=[], action="STOP", stop_reason="Report exported")
            return super().observe_round(plan, results, **kwargs).model_copy(update={
                "action": "CONTINUE", "stop_reason": None, "unresolved_questions": ("Export the committed report",)})
    agent = ReportAgent()
    retrieval(monkeypatch, agent)
    permissions = {}
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            source = InitialPlanningInput(user_query=UserQuery(query="Investigate and export", model_id="M"),
                available_capabilities=["KNOWLEDGE_RETRIEVAL", "COMPUTATION"])
            async with sessions.begin() as session:
                await ledger.register_investigation(session, InvestigationRecord(investigation_id="I", created_by="A", planning_input=source))
                await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
                await ledger.accept_plan(session, AcceptedPlan(investigation_id="I", round_id="R", plan_id="P", plan_version=1,
                    plan=InvestigationPlan(goal="Investigate", tasks=[InvestigationTask(task_id="T0", objective="Read background", expected_result="Clues", capability="KNOWLEDGE_RETRIEVAL")])))
            trusted = await context(sessions, permission=lambda a, op, r: permissions.get(op, True))
            trusted = replace(trusted, tools=(*trusted.tools, ReportExportTool(actor_id="A", model_id="M", directory=tmp_path)))
            first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=trusted))
            assert first["status"] == "WAITING_APPROVAL" and not list(tmp_path.iterdir())
            pending = first["pending_action"]
            assert pending["material"]["observation_ids"] and "Historical knowledge" in pending["material"]["markdown"]
            def bound_tools(*_):
                if permissions.get("restrict_knowledge"):
                    knowledge = trusted.tools[0]
                    restricted = replace(knowledge, execution=knowledge.execution.model_copy(update={
                        "scope": knowledge.execution.scope.model_copy(update={"document_id": "OTHER"})}))
                    return (restricted, *trusted.tools[1:])
                return trusted.tools
            services = EntryServices(sessions=sessions, runner=runner, chat_agent=None, tools_for=bound_tools,
                authorize_resource=trusted.authorize_resource, authorize_conversation=lambda *_: True)
            assert pending["action_id"] in await services._progress("A", "I")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(create_app(entry_services=services, authenticate=lambda _: "A")), base_url="http://test") as client:
                url = "/actions/" + pending["action_id"]
                confirmed = await client.post(url + "/confirmation", json={"digest": pending["digest"], "approved": True})
                assert confirmed.status_code == 200, confirmed.text
                response = await client.get(url + "/report")
                assert response.status_code == 200 and response.text == pending["material"]["markdown"]
                permissions["investigation:continue"] = False
                assert (await client.get(url + "/report")).status_code == 200
                permissions["restrict_knowledge"] = True
                assert (await client.get(url + "/report")).status_code == 403
                assert (await client.get(url)).status_code == 403
                permissions["restrict_knowledge"] = False
                permissions["action:download"] = False
                assert (await client.get(url + "/report")).status_code == 403
                assert len(list(tmp_path.iterdir())) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("manual", [False, True])
def test_uncertain_action_requires_authorized_evidence_or_external_verification(database, monkeypatch, manual):
    from risk_platform.execution import actions
    events, remote, permissions = [], {"crash": True}, {}
    name = "test_send"
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            pending = (await runner.ainvoke(plan_id="P", context=call))["pending_action"]
            action_id = pending["action_id"]
            await actions.decide(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource,
                digest=pending["digest"], approved=True)
            with pytest.raises(ProcessLost):
                await runner.ainvoke(plan_id="P", context=call)
            remote["unknown"] = True
            for _ in range(2):
                waiting = await runner.ainvoke(plan_id="P", context=call)
                assert waiting["status"] == "WAITING_VERIFICATION" and events.count("execute") == 1
            if manual:
                resolution = actions.ManualResolution(digest=pending["digest"], outcome="SUCCEEDED",
                    reason="Checked remote operation log", evidence="remote-operation:123", result=remote[action_id])
                permissions["action:resolve"] = False
                with pytest.raises(PermissionError):
                    await actions.resolve(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource, resolution=resolution)
                permissions["action:resolve"] = True
                await actions.resolve(sessions, action_id, actor="A", authorize=call.runtime.authorize_resource, resolution=resolution)
            else:
                remote["unknown"] = False
            completed = await runner.ainvoke(plan_id="P", context=call)
            assert completed["task_execution_results"][0].status == "SUCCEEDED" and events.count("execute") == 1
    asyncio.run(scenario())


def test_approval_policy_cannot_be_removed_while_waiting(database, monkeypatch):
    events, remote, permissions = [], {}, {}
    name = "test_update"
    definition = install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            pending = (await runner.ainvoke(plan_id="P", context=call))["pending_action"]
            from risk_platform.execution import actions
            await actions.decide(sessions, pending["action_id"], actor="A", authorize=call.runtime.authorize_resource,
                digest=pending["digest"], approved=True)
            monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType(dict(tools.TOOL_DEFINITIONS) |
                {name: replace(definition, approval=None)}))
            with pytest.raises((ValueError, PermissionError)):
                await runner.ainvoke(plan_id="P", context=call)
            assert "execute" not in events
    asyncio.run(scenario())


def test_multiple_actions_pause_in_task_order_and_reuse_previous_results(database, monkeypatch):
    from risk_platform.execution import actions
    events, remote, permissions = [], {}, {}
    name = "test_send"
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=2)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            for task_id in ("T0", "T1"):
                result = await runner.ainvoke(plan_id="P", context=call)
                pending = result["pending_action"]
                assert pending["task_id"] == task_id
                await actions.decide(sessions, pending["action_id"], actor="A", authorize=call.runtime.authorize_resource,
                    digest=pending["digest"], approved=True)
            done = await runner.ainvoke(plan_id="P", context=call)
            assert done["round_complete"] and events.count("execute") == 2 and events.count("query") == 2
    asyncio.run(scenario())


def test_approval_material_and_audit_are_immutable_in_database(database, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy import text
    events, remote, permissions = [], {}, {}
    name = "test_send"
    install(monkeypatch, events, remote, name)
    async def scenario():
        async with resources(database, Agent(name, events), setup=True) as (sessions, saver, runner):
            await seed(sessions, count=1)
            call = InvestigationInvocation(runtime=await runtime(sessions, name, permissions))
            pending = (await runner.ainvoke(plan_id="P", context=call))["pending_action"]
            for sql in ("UPDATE controlled_actions SET payload='{}' WHERE action_id=:id",
                        "DELETE FROM controlled_action_audit WHERE action_id=:id"):
                with pytest.raises(DBAPIError):
                    async with sessions.begin() as session:
                        await session.execute(text(sql), {"id": pending["action_id"]})
    asyncio.run(scenario())
