"""CSV 统计的计算、授权、注册、引用和持久化验收；不调用真实模型。"""
from pathlib import Path
import asyncio
import hashlib
import json
import os
from dataclasses import replace

import pytest
from pydantic import ValidationError

from risk_platform.execution import csv_statistics as csv_tool, tools
from risk_platform.execution.contracts import CsvStatisticsRequest, CsvStatisticsPayload
from risk_platform.workflow.contracts import (
    CapabilityType, InitialPlanningInput, UserQuery, InvestigationPlan, InvestigationTask,
)
from risk_platform.workflow.observation import ObservationDraft, complete_observation, project_tool_results
from risk_platform.workflow import nodes
from test_runtime import make_runtime
from workflow_test_helpers import run_phase


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "samples.csv"
    path.write_text("id_card,apply_time,is_bad,income\n"
        "same,2025-09-01 00:00:00,1,NA\n"
        "same,2025-09-15 12:00:00,0,2000\n"
        "other,2025-10-01 00:00:00,1,3000\n", encoding="utf-8-sig")
    return csv_tool.CsvDataset(dataset_id="train", model_id="M", path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), timezone_offset="+08:00",
        window_start="2025-09-01T00:00:00+08:00", window_end="2025-11-01T00:00:00+08:00",
        fields=("income",))


def request(statistic="BAD_LABEL_RATE", **overrides):
    return CsvStatisticsRequest.model_validate(dict(dataset_id="train", statistic=statistic,
        window_start="2025-09-01T00:00:00+08:00", window_end="2025-10-01T00:00:00+08:00") | overrides)


def source():
    return InitialPlanningInput(user_query=UserQuery(query="统计 train 的九月坏标签占比", model_id="M"),
                                available_capabilities=["COMPUTATION"])


def runtime(dataset):
    return replace(make_runtime(source()), tools=(csv_tool.CsvStatisticsTool(
        actor_id="actor", model_id="M", datasets=(dataset,)),))


def task():
    return InvestigationTask(task_id="T", objective="统计 train 九月坏标签占比",
        expected_result="取得分子分母和比例", capability="COMPUTATION")


@pytest.mark.parametrize("statistic,field,numerator,value", [
    ("SAMPLE_COUNT", None, 2, 2), ("BAD_LABEL_RATE", None, 1, .5), ("MISSING_RATE", "income", 1, .5),
])
def test_counts_boundaries_and_missing_markers(dataset, statistic, field, numerator, value):
    result = csv_tool.scan_dataset(dataset, request(statistic, field=field))
    assert result.sample_count == 2 and result.numerator == numerator and result.value == value
    assert result.dataset_sha256 == dataset.sha256
    # 重复脱敏证件仍按两条申请记录计数；结束时刻的十月记录不属于九月。


@pytest.mark.parametrize("statistic,value", [("SAMPLE_COUNT", 0), ("BAD_LABEL_RATE", None)])
def test_empty_window_is_not_zero_rate(dataset, statistic, value):
    result = csv_tool.scan_dataset(dataset, request(statistic,
        window_start="2025-10-02T00:00:00+08:00", window_end="2025-10-03T00:00:00+08:00"))
    assert result.outcome == "NO_ROWS" and result.sample_count == 0 and result.value == value


@pytest.mark.parametrize("patch", [
    {"field": "income"}, {"statistic": "MISSING_RATE"}, {"statistic": "SQL"},
    {"window_start": "2025-10-01T00:00:00+08:00"}, {"path": "other.csv"},
    {"window_start": "2025-09-01T00:00:00"},
])
def test_request_contract(patch):
    with pytest.raises(ValidationError):
        request(**patch)


@pytest.mark.parametrize("patch", [
    {"dataset_id": "test"}, {"statistic": "MISSING_RATE", "field": "id_card"},
    {"window_start": "2025-08-01T00:00:00+08:00"},
])
def test_authorized_ranges(dataset, patch):
    with pytest.raises(PermissionError):
        tools.validate_tool_call(tools.ToolCall("csv_sample_statistics", request(**patch)),
                                 CapabilityType.COMPUTATION, runtime(dataset))


def test_actor_and_model_binding(dataset):
    bound = runtime(dataset)
    from risk_platform.workflow.runtime import check_runtime, InvestigationAction
    for changed in (replace(bound, actor_id="other"), replace(bound,
        tools=(csv_tool.CsvStatisticsTool(actor_id="actor", model_id="OTHER",
            datasets=(dataset.model_copy(update={"model_id": "OTHER"}),)),))):
        with pytest.raises(PermissionError):
            check_runtime(changed, InvestigationAction.CONTINUE)


def test_failure_does_not_leak_file_path(dataset):
    dataset.path.write_text("modified", encoding="utf-8")
    result = asyncio.run(tools.CSV_SAMPLE_STATISTICS.tool.ainvoke(request().model_dump(mode="json"),
        config={"configurable": {"task": task(), "runtime": runtime(dataset)}}))
    assert result.status == "FAILED" and result.error.code == "CSV_VERSION_CHANGED"
    assert result.payload is None and str(dataset.path) not in result.model_dump_json()


@pytest.mark.parametrize("raw", [
    "apply_time,is_bad,income\ninvalid,0,1\n",
    "apply_time,is_bad,income\n2025-10-01 00:00:00,2,1\n",
    "apply_time,is_bad\n2025-09-01 00:00:00,0\n",
    "apply_time,is_bad,income\n2025-09-01 00:00:00,0\n",
    "apply_time,is_bad,income\n2025-08-01 00:00:00,0,1\n",
])
def test_bad_rows_fail_even_outside_requested_window(dataset, raw):
    dataset.path.write_text(raw, encoding="utf-8")
    dataset = dataset.model_copy(update={"sha256": hashlib.sha256(dataset.path.read_bytes()).hexdigest()})
    result = asyncio.run(tools.CSV_SAMPLE_STATISTICS.tool.ainvoke(request().model_dump(mode="json"), config={"configurable": {"task": task(), "runtime": runtime(dataset)}}))
    assert result.status == "FAILED" and result.error.code == "CSV_INVALID"


def test_result_validation_and_reference(dataset):
    result = csv_tool.scan_dataset(dataset, request())
    for patch in ({"value": .9}, {"numerator": 3}, {"outcome": "NO_ROWS"}):
        with pytest.raises(ValidationError):
            CsvStatisticsPayload.model_validate(result.model_dump() | patch)
    with pytest.raises(ValueError):
        csv_tool.validate_result(request(), csv_tool.execution_limits(runtime(dataset)),
            result.model_copy(update={"dataset_sha256": "0" * 64}))
    csv_tool.validate_reference({"reference_id": "aggregate"}, result)
    with pytest.raises(ValueError):
        csv_tool.validate_reference({"reference_id": "invented"}, result)
    assert "path" not in json.dumps(csv_tool.execution_limits(runtime(dataset)))


class Agent:
    from risk_platform.agents.skills import SkillDirectory
    from risk_platform.agents import investigation as agent_module
    skill_directory = SkillDirectory(Path(agent_module.__file__).with_name("skill_library"))
    model = "test-model"

    def __init__(self):
        self.calls = []

    def initial_plan(self, planning_input, **kwargs):
        self.calls.append("plan")
        assert CapabilityType.COMPUTATION in planning_input.available_capabilities
        return InvestigationPlan(goal="统计离线样本", tasks=[task()])

    def formulate_tool_call(self, *args, **kwargs):
        self.calls.append("query")
        assert [t.name for t in kwargs["tools"]] == ["csv_sample_statistics"]
        assert "path" not in json.dumps(kwargs["tool_limits"])
        return tools.ToolCall("csv_sample_statistics", request())

    def observe_round(self, plan, results, **kwargs):
        self.calls.append("observe")
        assert project_tool_results(results)[0]["items"][0]["content"]["value"] == .5
        return ObservationDraft(findings=[dict(statement="离线窗口坏标签占比为 0.5，不证明线上坏账率",
            sources=[dict(execution_id=results[0].execution_id, reference={"reference_id": "aggregate"})])],
            unresolved_questions=[], action="STOP", stop_reason="统计问题已回答")


def test_registered_tool_through_business_nodes(dataset):
    agent = Agent()
    async def scenario():
        state = dict(planning_input=source(), investigation_plan=agent.initial_plan(source()))
        state.update(await nodes.task_execution(state, investigation_agent=agent, runtime_context=runtime(dataset),
                                               query_phase=run_phase, tool_phase=run_phase))
        state.update(await nodes.observation(state, investigation_agent=agent, runtime_context=runtime(dataset),
                                             observation_phase=run_phase))
        assert state["observation"].status == "SUCCEEDED"
        bad = agent.observe_round(None, state["task_execution_results"])
        bad.findings[0].sources[0].reference["reference_id"] = "other"
        with pytest.raises(ValueError):
            complete_observation(state["task_execution_results"], bad)
    asyncio.run(scenario())


@pytest.mark.skipif(os.environ.get("RUN_POSTGRES_INTEGRATION") != "1", reason="requires isolated PostgreSQL")
@pytest.mark.parametrize("interrupt", [False, True])
def test_csv_persists_and_recovers_without_rescan(database, dataset, monkeypatch, interrupt):
    from sqlalchemy import select
    from risk_platform.workflow import phase_execution
    from risk_platform.workflow.access import load_runtime_context
    from risk_platform.workflow.runtime import InvestigationAction
    from risk_platform.workflow.invocation import InvestigationInvocation
    from risk_platform.workflow.persistence import repository as ledger
    from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord
    from risk_platform.workflow.persistence.models import ResultRow
    from test_recovery_postgres import resources, ProcessLost
    agent = Agent()
    original = phase_execution._phase
    crashed = []

    async def phase(*args, **kwargs):
        result = await original(*args, **kwargs)
        if interrupt and args[2].operation_type == "TOOL_EXECUTION" and not crashed:
            crashed.append(True)
            raise ProcessLost()
        return result
    monkeypatch.setattr(phase_execution, "_phase", phase)

    async def bound(sessions):
        async with sessions() as session:
            return await load_runtime_context(session, investigation_id="I", round_id="R", actor_id="actor",
                authorize_resource=lambda *_: True, tools=runtime(dataset).tools,
                action=InvestigationAction.CONTINUE)

    async def scenario():
        async with resources(database, agent, setup=True) as (sessions, saver, runner):
            async with sessions.begin() as session:
                await ledger.register_investigation(session, InvestigationRecord(
                    investigation_id="I", created_by="actor", planning_input=source()))
                await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
            if interrupt:
                with pytest.raises(ProcessLost):
                    await runner.ainvoke(context=InvestigationInvocation(runtime=await bound(sessions)))
            else:
                first = await runner.ainvoke(context=InvestigationInvocation(runtime=await bound(sessions)))
        before = list(agent.calls)
        def forbidden(*args):
            pytest.fail("可靠提交结果恢复后不应扫描文件")
        monkeypatch.setattr(csv_tool, "scan_dataset", forbidden)
        async with resources(database, agent) as (sessions, saver, runner):
            output = await runner.ainvoke(context=InvestigationInvocation(runtime=await bound(sessions)))
            assert output["observation"].status == "SUCCEEDED"
            assert output["task_execution_results"][0].payload["value"] == .5
            assert agent.calls == before + (["observe"] if interrupt else [])
            async with sessions() as session:
                assert len(list(await session.scalars(select(ResultRow)))) == 3
            if not interrupt:
                assert output == first
    asyncio.run(scenario())


# Fixture uses isolated schemas, never the application's business schema.
from test_ledger_postgres import database
