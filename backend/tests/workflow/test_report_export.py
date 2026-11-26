import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_first_round_execution import state
from test_runtime import make_runtime


def test_export_writes_only_frozen_material_and_verifies_existing_file(tmp_path):
    from risk_platform.execution import report_export as report
    from risk_platform.execution import tools
    source = state(1)
    runtime = replace(make_runtime(source["planning_input"]), tools=(
        report.ReportExportTool(actor_id="actor", model_id="M", directory=tmp_path),))
    material = report.ReportMaterial(markdown="# Frozen\nNo raw rows.\n", observation_ids=["O1"])
    task = source["investigation_plan"].tasks[0].model_copy(update={"capability": "COMPUTATION"})
    async def scenario():
        cfg = {"configurable": {"runtime": runtime, "task": task,
            "action_id": "action-test", "approved_material": material}}
        result = await tools.REPORT_EXPORT.tool.ainvoke({"investigation_id": runtime.investigation.investigation_id}, config=cfg)
        data = (tmp_path / "action-test.md").read_bytes()
        assert data == b"# Frozen\nNo raw rows.\n"
        assert result.payload["sha256"] == hashlib.sha256(data).hexdigest()
        verified = await report.verify(runtime, material, "action-test")
        assert verified == report.ReportReceipt.model_validate(result.payload)
        assert list(tmp_path.iterdir()) == [tmp_path / "action-test.md"]
    asyncio.run(scenario())


def test_export_rejects_existing_different_content_and_symlinks(tmp_path):
    from risk_platform.execution import report_export as report
    runtime = replace(make_runtime(state(1)["planning_input"]), tools=(
        report.ReportExportTool(actor_id="actor", model_id="M", directory=tmp_path),))
    (tmp_path / "action-test.md").write_text("wrong", encoding="utf-8")
    assert asyncio.run(report.verify(runtime, report.ReportMaterial(markdown="approved", observation_ids=["O"]), "action-test")) == "UNKNOWN"
    with pytest.raises(ValueError):
        report.artifact_path(tmp_path, "../escape")


def test_model_is_not_offered_export_when_request_permission_is_denied(tmp_path):
    from risk_platform.execution import tools
    from risk_platform.execution.report_export import ReportExportTool
    from risk_platform.workflow.contracts import CapabilityType
    runtime = make_runtime(state(1)["planning_input"], authorize_resource=lambda actor, action, resource: action != "action:request")
    runtime = replace(runtime, tools=(ReportExportTool(actor_id="actor", model_id="M", directory=tmp_path),))
    with pytest.raises(tools.ToolUnavailableError):
        tools.available_tools(CapabilityType.COMPUTATION, runtime)
