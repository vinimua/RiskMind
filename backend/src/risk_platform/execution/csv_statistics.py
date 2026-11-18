"""授权数据集的只读基础统计；不训练模型，不接受文件路径或任意表达式。"""

import asyncio
import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator
from langchain_core.runnables import RunnableConfig

from risk_platform.execution.contracts import (
    ExecutionContract, CsvStatisticsRequest, CsvStatisticsPayload, CsvStatisticsReference,
    ExecutionError, TaskExecutionResult,
)
from risk_platform.workflow.runtime import InvestigationAccessError

RULES_VERSION = "csv-statistics/v1"
MISSING_MARKERS = frozenset(("", "na", "nan", "null", "none"))
MAX_FILE_BYTES = 64 * 1024 * 1024


class CsvDataset(ExecutionContract):
    """仅由组合入口配置；路径不进入工具声明、公开限制或持久化结果。"""
    dataset_id: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    model_id: str = Field(min_length=1)
    path: Path = Field(repr=False)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    timezone_offset: str = Field(pattern=r"^[+-]\d{2}:\d{2}$")
    window_start: AwareDatetime
    window_end: AwareDatetime
    fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_dataset(self):
        datetime.strptime(self.timezone_offset, "%z")
        if self.window_start >= self.window_end:
            raise ValueError("数据集授权窗口必须非空")
        if len(set(self.fields)) != len(self.fields) or any(
            not name.strip() or name in {"id_card", "apply_time", "is_bad"} for name in self.fields
        ):
            raise ValueError("统计字段必须唯一，不能包含身份、时间或标签字段")
        return self


@dataclass(frozen=True)
class CsvStatisticsTool:
    actor_id: str
    model_id: str
    datasets: tuple[CsvDataset, ...]
    name: ClassVar[str] = "csv_sample_statistics"

    def __post_init__(self):
        if not self.actor_id.strip() or not self.datasets:
            raise ValueError("CSV 工具必须有明确身份和数据集")
        if len({d.dataset_id for d in self.datasets}) != len(self.datasets):
            raise ValueError("数据集 ID 不能重复")
        if any(d.model_id != self.model_id for d in self.datasets):
            raise ValueError("数据集与绑定模型不一致")

    def validate_binding(self, actor_id, resource):
        if actor_id != self.actor_id or resource.model_id != self.model_id:
            raise InvestigationAccessError("CSV 数据集不属于本次身份或调查模型")


def binding(runtime) -> CsvStatisticsTool:
    for tool in runtime.tools:
        if isinstance(tool, CsvStatisticsTool):
            return tool
    raise InvestigationAccessError("本次运行没有 CSV 统计绑定")


def execution_limits(runtime):
    """固定计算所依赖的数据版本和解释规则；不暴露文件位置。"""
    return {"rules_version": RULES_VERSION, "datasets": [
        d.model_dump(mode="json", exclude={"path"}) for d in binding(runtime).datasets
    ]}


def validate_request(runtime, request):
    allowed = next((d for d in binding(runtime).datasets if d.dataset_id == request.dataset_id), None)
    if allowed is None:
        raise InvestigationAccessError("数据集不在本次授权范围内")
    if not (allowed.window_start <= request.window_start < request.window_end <= allowed.window_end):
        raise InvestigationAccessError("统计窗口超出数据集授权范围")
    if request.field is not None and request.field not in allowed.fields:
        raise InvestigationAccessError("统计字段不在本次授权范围内")


def calculate_statistics(rows, request):
    """只接收标准化记录和查询；按申请记录计数，不按脱敏证件去重。"""
    count, numerator = 0, 0
    for applied_at, label, fields in rows:
        if request.window_start <= applied_at < request.window_end:
            count += 1
            if request.statistic == "SAMPLE_COUNT":
                numerator += 1
            elif request.statistic == "BAD_LABEL_RATE":
                numerator += label
            else:
                numerator += fields[request.field].strip().casefold() in MISSING_MARKERS
    value = count if request.statistic == "SAMPLE_COUNT" else numerator / count if count else None
    return count, numerator, value


def _records(raw, dataset):
    """整份数据的结构、时间和标签均校验，不静默忽略窗口外的坏记录。"""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    columns = reader.fieldnames
    required = {"apply_time", "is_bad", *dataset.fields}
    if not columns or len(columns) != len(set(columns)) or not required <= set(columns):
        raise ValueError("CSV 列缺失或重复")
    tz = datetime.strptime(dataset.timezone_offset, "%z").tzinfo
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("CSV 行列数量不一致")
        applied_at = datetime.strptime(row["apply_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        if not dataset.window_start <= applied_at < dataset.window_end:
            raise ValueError("CSV 记录超出声明的数据窗口")
        if row["is_bad"] not in {"0", "1"}:
            raise ValueError("CSV 标签必须为 0 或 1")
        yield applied_at, int(row["is_bad"]), row


class DatasetChangedError(ValueError):
    pass


def scan_dataset(dataset, request):
    # 只打开一次；摘要核验和解析针对同一份字节，不存在二次打开的版本窗口。
    with dataset.path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("CSV 超过单次扫描大小限制")
    if hashlib.sha256(raw).hexdigest() != dataset.sha256:
        raise DatasetChangedError("CSV 内容版本已变化")
    count, numerator, value = calculate_statistics(_records(raw, dataset), request)
    return CsvStatisticsPayload(request=request, dataset_sha256=dataset.sha256,
        outcome="VALUE_AVAILABLE" if count else "NO_ROWS", sample_count=count,
        numerator=numerator, value=value)


async def sample_statistics(config: RunnableConfig, **arguments):
    from risk_platform.workflow.runtime import InvestigationAction, check_runtime
    runtime_context = config["configurable"]["runtime"]
    task = config["configurable"]["task"]
    check_runtime(runtime_context, InvestigationAction.CONTINUE)
    request = CsvStatisticsRequest.model_validate(arguments)
    validate_request(runtime_context, request)
    dataset = next(d for d in binding(runtime_context).datasets if d.dataset_id == request.dataset_id)
    started = datetime.now(timezone.utc)
    payload, error = None, None
    try:
        async with asyncio.timeout(runtime_context.timeout_seconds):
            payload = await asyncio.to_thread(scan_dataset, dataset, request)
    except DatasetChangedError:
        error = ExecutionError(category="INTEGRITY", code="CSV_VERSION_CHANGED",
            message="数据版本变化，未返回统计结果", retryable=False)
    except (ValueError, UnicodeError, csv.Error):
        error = ExecutionError(category="VALIDATION", code="CSV_INVALID",
            message="CSV 结构、时间或标签非法，未返回统计结果", retryable=False)
    except OSError:
        error = ExecutionError(category="DEPENDENCY", code="CSV_UNAVAILABLE",
            message="授权数据文件不可读取", retryable=False)
    except TimeoutError:
        error = ExecutionError(category="DEPENDENCY", code="CSV_TIMEOUT",
            message="CSV 统计超时，未返回部分结果", retryable=True)
    return TaskExecutionResult(execution_id="exec-" + uuid4().hex, task_id=task.task_id,
        capability=task.capability, tool_name=CsvStatisticsTool.name, result_version="1",
        status="FAILED" if error else "SUCCEEDED", started_at=started,
        completed_at=datetime.now(timezone.utc), payload=payload.model_dump(mode="json") if payload else None,
        error=error)


def validate_result(request, limits, result):
    dataset = next((d for d in limits["datasets"] if d["dataset_id"] == request.dataset_id), None)
    if dataset is None or result.request != request or result.dataset_sha256 != dataset["sha256"]:
        raise ValueError("统计结果与请求或数据版本不一致")
    if limits["rules_version"] != result.rules_version:
        raise ValueError("统计规则版本不一致")


def authorize_result(runtime, result):
    if result.payload is None:
        return True
    payload = CsvStatisticsPayload.model_validate(result.payload)
    validate_request(runtime, payload.request)
    validate_result(payload.request, execution_limits(runtime), payload)
    return True


def observation_items(result):
    return [{"reference_id": "aggregate", "content": {
        **result.model_dump(mode="json"),
        "definition": {"SAMPLE_COUNT": "窗口内申请记录数，不按身份证去重",
            "BAD_LABEL_RATE": "窗口内 is_bad=1 的记录数 / 窗口记录数；不证明标签成熟或线上坏账率",
            "MISSING_RATE": "指定字段去除首尾空格后，空串或 na/nan/null/none（忽略大小写）的记录数 / 窗口记录数",
        }[result.request.statistic],
    }}]


def validate_reference(reference, result):
    CsvStatisticsReference.model_validate(reference)
