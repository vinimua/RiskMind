"""真实账本与检查点集成测试；模型和检索使用替身，不产生外部费用。"""
import asyncio
import json
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
import pytest

from test_ledger_postgres import database, pytestmark
from test_initial_planning_postgres import Planner, seed_round, call
from test_recovery_postgres import resources, retrieval, ProcessLost
from risk_platform.workflow.persistence.planning_models import PlanningDialogueRow
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy
from risk_platform.workflow.planning import PlanningPreparationError, planning_methods
from risk_platform.workflow.contracts import SkillMaterialRef
from risk_platform.workflow import planning as planning_module


class SkillPlanner(Planner):
    crash_after_read = False

    def initial_plan(self, source, *, skill_catalogue, skill_messages, **kwargs):
        self.events.append("planning-turn")
        assert len(skill_catalogue) == 3 and all("content" not in item for item in skill_catalogue)
        if not skill_messages:
            return {"role": "assistant", "tool_calls": [{"id": "read-1", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": "feature-drift-investigation"})}}]}
        assert skill_messages[-1]["role"] == "tool"
        result = json.loads(skill_messages[-1]["content"])
        assert result["kind"] == "METHOD_NOT_CASE_EVIDENCE"
        if self.crash_after_read:
            self.crash_after_read = False
            raise ProcessLost("read committed; next model reply not saved")
        plan = super().initial_plan(source)
        plan.tasks[0].skill_refs = (SkillMaterialRef(skill_name=result["name"], resource=result["resource"]),)
        return plan

    def formulate_tool_call(self, task, *args, **options):
        if task.skill_refs:
            assert [(m["name"], m["resource"]) for m in options["skill_materials"]] == [
                (r.skill_name, r.resource) for r in task.skill_refs]
        else:
            assert not options.get("skill_materials")
        return super().formulate_tool_call(task, *args, **options)

    def observe_round(self, plan, results, **options):
        assert options["skill_materials"][0]["name"] == "feature-drift-investigation"
        return super().observe_round(plan, results, **options)


@pytest.mark.parametrize("interrupt", [False, True])
def test_read_commit_restore_and_observation_share_fixed_material(database, monkeypatch, interrupt):
    agent = SkillPlanner()
    agent.crash_after_read = interrupt
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            if interrupt:
                with pytest.raises(ProcessLost):
                    await call(runner, sessions)
            else:
                assert (await call(runner, sessions))["round_complete"]
        async with resources(database, agent) as (sessions, saver, runner):
            result = await call(runner, sessions, policy=RecoveryPolicy(retry_uncertain=True))
            assert result["round_complete"]
            async with sessions() as session:
                rows = list(await session.scalars(select(PlanningDialogueRow).order_by(PlanningDialogueRow.sequence)))
                assert [json.loads(row.payload)["kind"] for row in rows if json.loads(row.payload)["kind"] != "calling"] == ["catalogue", "model", "read", "plan"]
                assert sum(event == "initial-plan" for event in agent.events) == 1
            with pytest.raises(DBAPIError):
                async with sessions.begin() as session:
                    await session.execute(text("UPDATE investigation_planning_dialogue SET payload='{}' WHERE sequence=0"))
    asyncio.run(scenario())


def test_repeated_bad_read_never_accepts_plan(database, monkeypatch):
    agent = SkillPlanner()
    def invalid(source, **kwargs):
        return {"role": "assistant", "tool_calls": [{"id": "bad", "type": "function",
            "function": {"name": "read_skill", "arguments": '{"name":"unknown"}'}}]}
    agent.initial_plan = invalid
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(PlanningPreparationError):
                await call(runner, sessions)
            async with sessions() as session:
                rows = list(await session.scalars(select(PlanningDialogueRow)))
                assert not any(json.loads(row.payload)["kind"] == "plan" for row in rows)
    asyncio.run(scenario())


@pytest.mark.parametrize("exhaust", [False, True])
def test_multiple_skills_references_and_read_limit(database, monkeypatch, exhaust):
    agent = SkillPlanner()
    def model(source, *, skill_messages, **kwargs):
        reads = [json.loads(m["content"]) for m in skill_messages if m["role"] == "tool"]
        if len(reads) >= 3 and not exhaust:
            assert [r["resource"] for r in reads] == ["SKILL.md", "SKILL.md", "references/psi.md"]
            plan = Planner.initial_plan(agent, source)
            plan.tasks[0].skill_refs = tuple(SkillMaterialRef(skill_name=r["name"], resource=r["resource"]) for r in reads)
            return plan
        requests = ([{"name": "feature-drift-investigation"}, {"name": "data-quality-investigation"}]
                    if not reads else [{"name": "feature-drift-investigation", "resource": "references/psi.md"}])
        return {"role": "assistant", "tool_calls": [dict(id=f"read-{len(reads)}-{i}", type="function",
            function=dict(name="read_skill", arguments=json.dumps(request))) for i, request in enumerate(requests)]}
    agent.initial_plan = model
    retrieval(monkeypatch, agent)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            if exhaust:
                with pytest.raises(PlanningPreparationError):
                    await call(runner, sessions)
            else:
                assert (await call(runner, sessions))["round_complete"]
            async with sessions() as session:
                events = [json.loads(r.payload) for r in await session.scalars(select(PlanningDialogueRow).order_by(PlanningDialogueRow.sequence))]
                assert len(planning_methods(events)) == 3
                # 重复请求只保存已加载提示，不能重复追加原文。
                bodies = [json.loads(m["content"]) for e in events if e["kind"] == "read" for m in e["messages"]]
                assert sum("content" in body for body in bodies) == 3
                assert any(e["kind"] == "plan" for e in events) is not exhaust
    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["prepare_plan", "read_skill_material"])
def test_main_graph_reuses_committed_read_boundary_after_lost_checkpoint(database, monkeypatch, boundary):
    agent = SkillPlanner()
    retrieval(monkeypatch, agent)
    original = getattr(planning_module, boundary)
    crashed = False
    async def interrupt(*args, **kwargs):
        nonlocal crashed
        update = await original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise ProcessLost("步骤已提交，主图检查点尚未保存")
        return update
    monkeypatch.setattr(planning_module, boundary, interrupt)
    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            await seed_round(sessions)
            with pytest.raises(ProcessLost):
                await call(runner, sessions)
        async with resources(database, agent) as (sessions, saver, runner):
            assert (await call(runner, sessions))["round_complete"]
            assert agent.events.count("planning-turn") == 2
            async with sessions() as session:
                rows = await session.scalars(select(PlanningDialogueRow).order_by(PlanningDialogueRow.sequence))
                assert [json.loads(row.payload)["kind"] for row in rows if json.loads(row.payload)["kind"] != "calling"] == ["catalogue", "model", "read", "plan"]
    asyncio.run(scenario())
