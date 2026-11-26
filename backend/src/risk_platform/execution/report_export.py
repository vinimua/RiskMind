"""将已批准的调查材料原子写入专用目录；恢复只核实同一份文件。"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile

from langchain_core.runnables import RunnableConfig
from pydantic import Field
from sqlalchemy import select

from risk_platform.execution.contracts import ExecutionContract, TaskExecutionResult
from risk_platform.workflow.runtime import InvestigationAction, InvestigationAccessError, check_runtime


class ReportRequest(ExecutionContract):
    investigation_id: str = Field(min_length=1, max_length=128)


class ReportMaterial(ExecutionContract):
    markdown: str = Field(min_length=1, max_length=2_000_000)
    observation_ids: list[str] = Field(min_length=1)


class ReportReceipt(ExecutionContract):
    artifact_id: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class ReportReference(ExecutionContract):
    reference_id: str


@dataclass(frozen=True)
class ReportExportTool:
    actor_id: str
    model_id: str
    directory: Path
    name = "export_investigation_report"

    def validate_binding(self, actor, resource):
        if actor != self.actor_id or resource.model_id != self.model_id or not self.directory.is_absolute():
            raise InvestigationAccessError("Report directory or actor is outside grant")


def binding(runtime):
    for tool in runtime.tools:
        if isinstance(tool, ReportExportTool):
            return tool
    raise InvestigationAccessError("Report export is not bound")


def artifact_path(directory, action_id):
    if not re.fullmatch(r"action-[A-Za-z0-9_-]+", action_id):
        raise ValueError("Invalid artifact identity")
    path = directory / (action_id + ".md")
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise InvestigationAccessError("Artifact is outside report storage")
    return path


def validate_request(runtime, request):
    binding(runtime)
    if request.investigation_id != runtime.investigation.investigation_id:
        raise InvestigationAccessError("Cannot export another investigation")


async def read_observations(sessions, runtime, observation_ids=None):
    """准备与下载共用来源授权；只读取批准所关联的正式记录，不混入新材料。"""
    from risk_platform.workflow.persistence.models import ResultRow, OperationRow
    from risk_platform.workflow.persistence import repository as ledger
    from risk_platform.workflow.access import load_runtime_context, _check_registered_result
    observations = []
    async with sessions() as session:
        query = select(ResultRow).join(OperationRow,
            ResultRow.operation_key == OperationRow.operation_key).where(
            OperationRow.investigation_id == runtime.investigation.investigation_id,
            ResultRow.observation_id.is_not(None)).order_by(ResultRow.created_at, ResultRow.result_id)
        if observation_ids is not None:
            query = query.where(ResultRow.observation_id.in_(observation_ids))
        rows = list(await session.scalars(query))
        if observation_ids is not None and set(observation_ids) != {row.observation_id for row in rows}:
            raise InvestigationAccessError("Approved report sources are unavailable")
        for row in rows:
            operation = ledger.read_operation(await session.get(OperationRow, row.operation_key))
            historical = await load_runtime_context(session, investigation_id=operation.investigation_id,
                round_id=operation.round_id, actor_id=runtime.actor_id, tools=runtime.tools,
                authorize_resource=runtime.authorize_resource, action=InvestigationAction.READ,
                timeout_seconds=runtime.timeout_seconds)
            for result in await ledger._observation_results(session, operation):
                _check_registered_result(result, historical)
            observations.append(ledger.read_artifact(row).observation)
    return observations


async def prepare(sessions, runtime, request):
    validate_request(runtime, request)
    sections, ids = [], []
    for observation in await read_observations(sessions, runtime):
        ids.append(observation.observation_id)
        sections.append("## Observation " + observation.observation_id)
        for finding in observation.findings:
            sections.append("- " + finding.statement)
            sections.extend("  - Evidence execution: " + source.execution_id for source in finding.sources)
        sections.append("### Unresolved questions")
        sections.extend("- " + question for question in observation.unresolved_questions)
        sections.append("Stop reason: " + (observation.stop_reason or "Investigation continues"))
    if not ids:
        raise ValueError("No committed observations to export")
    return ReportMaterial(markdown="# Investigation " + request.investigation_id + "\n\n" + "\n\n".join(sections) + "\n",
        observation_ids=ids)


async def precondition(sessions, runtime, material):
    binding(runtime)
    check_runtime(runtime, InvestigationAction.READ)
    await read_observations(sessions, runtime, material.observation_ids)
    return True


def _write(directory, action_id, content):
    directory.mkdir(parents=True, exist_ok=True)
    destination = artifact_path(directory, action_id)
    raw = content.encode("utf-8")
    if destination.exists():
        if destination.read_bytes() != raw:
            raise ValueError("Existing artifact differs from approved content")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        # 同卷硬链接原子安装完整文件，不覆盖另一个执行者已经生成的产物。
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != raw:
                raise ValueError("Artifact collision")
    finally:
        Path(temporary).unlink(missing_ok=True)


async def verify(runtime, material, action_id):
    path = artifact_path(binding(runtime).directory, action_id)
    if not path.exists():
        return "NOT_APPLIED"
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return "UNKNOWN"
    expected = material.markdown.encode("utf-8")
    if raw != expected:
        return "UNKNOWN"
    return ReportReceipt(artifact_id=action_id, sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))


async def export_report(config: RunnableConfig, **arguments):
    cfg = config["configurable"]
    runtime, task = cfg["runtime"], cfg["task"]
    check_runtime(runtime, InvestigationAction.CONTINUE)
    validate_request(runtime, ReportRequest.model_validate(arguments))
    material = ReportMaterial.model_validate(cfg["approved_material"])
    started = datetime.now(timezone.utc)
    await asyncio.to_thread(_write, binding(runtime).directory, cfg["action_id"], material.markdown)
    receipt = await verify(runtime, material, cfg["action_id"])
    if not isinstance(receipt, ReportReceipt):
        raise RuntimeError("Report publication could not be verified")
    return TaskExecutionResult(execution_id="exec-" + cfg["action_id"], task_id=task.task_id,
        capability=task.capability, tool_name=ReportExportTool.name, result_version="1", status="SUCCEEDED",
        started_at=started, completed_at=datetime.now(timezone.utc), payload=receipt.model_dump(mode="json"))


def validate_result(request, limits, payload):
    if request.investigation_id != limits["investigation_id"]:
        raise ValueError("Export belongs to another investigation")


def authorize_result(runtime, result):
    binding(runtime)
    return True


def validate_reference(reference, result):
    if ReportReference.model_validate(reference).reference_id != result.artifact_id:
        raise ValueError("Report receipt reference is not real")
