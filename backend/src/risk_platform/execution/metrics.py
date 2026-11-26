"""正式指标的只读协议与 MCP 边界；不计算指标，不提供默认身份或测试数据。"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from langchain_core.runnables import RunnableConfig
from risk_platform.core.token_budget import request_tokens
from risk_platform.execution.contracts import TaskExecutionResult, ExecutionError
from risk_platform.workflow.contracts import CapabilityType

QUERY_INSTRUCTIONS = """指标查询前必须读取当前任务的指标目录。每次只能请求一个工具。
目录返回不是查询事实；依据其中口径版本、支持窗口与分组填写最终查询。
允许受限补读目录，不能编造口径或用每日指标平均替代精确窗口。"""

class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DefinitionRequest(Contract):
    model_id: str = Field(min_length=1)
    metric: str | None = None
    cursor: str | None = None


class MetricQuery(Contract):
    model_id: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    start_date: date
    end_date: date
    group_by: str = "all"
    cursor: str | None = None

    @model_validator(mode="after")
    def window(self):
        if not 1 <= (self.end_date-self.start_date).days <= 93:
            raise ValueError("窗口必须为左闭右开的1至93天")
        return self


class MetricDefinition(Contract):
    model_id: str
    metric: str
    version: str
    description: str
    groups: tuple[str, ...]
    windows: tuple[tuple[date, date], ...]


class DefinitionPage(Contract):
    model_id: str
    items: tuple[MetricDefinition, ...]
    next_cursor: str | None = None
    data_kind: Literal["FORMAL", "TEST"]


class MetricItem(Contract):
    reference_id: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    group: str
    value: float = Field(allow_inf_nan=False)
    sample_count: int = Field(ge=0, strict=True)
    computed_at: datetime
    details: dict = Field(default_factory=dict)


class MetricPage(Contract):
    request: MetricQuery
    data_kind: Literal["FORMAL", "TEST"]
    status: Literal["AVAILABLE", "NO_DATA", "NOT_READY"]
    result_set_version: str = Field(min_length=1)
    items: tuple[MetricItem, ...]
    next_cursor: str | None = None

    @model_validator(mode="after")
    def identities(self):
        ids = [item.reference_id for item in self.items]
        if len(ids) != len(set(ids)) or (self.status != "AVAILABLE" and (ids or self.next_cursor)):
            raise ValueError("结果身份重复或未就绪结果含正文")
        if self.status == "AVAILABLE" and not ids:
            raise ValueError("可用结果不能为空")
        if any(item.computed_at.tzinfo is None for item in self.items):
            raise ValueError("计算时间必须包含时区")
        return self


class MetricReference(Contract):
    reference_id: str


class MetricsServiceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class MetricsClient:
    """连接按身份创建；发现时核对声明，调用时校验结构化返回及大小。"""
    def __init__(self, session, *, max_tokens=4000):
        self.session, self.max_tokens = session, max_tokens

    async def discover(self):
        result = await self.session.list_tools()
        tools = {tool.name: tool for tool in result.tools}
        for name, schema in (("list_metric_definitions", DefinitionRequest), ("query_model_metrics", MetricQuery)):
            if name not in tools:
                raise ValueError("指标服务缺少注册工具")
            expected = schema.model_json_schema()
            wrapper = tools[name].input_schema
            actual = wrapper.get("$defs", {}).get(schema.__name__, {})
            if actual.get("properties") != expected.get("properties") or actual.get("required", []) != expected.get("required", []):
                raise ValueError("远端指标参数协议与本地契约不一致")

    async def call(self, name, request):
        if name not in {"list_metric_definitions", "query_model_metrics"}:
            raise ValueError("MCP工具不在白名单")
        result = await self.session.call_tool(name, {"request": request.model_dump(mode="json")})
        if result.is_error:
            text = " ".join(getattr(item, "text", "") for item in result.content)
            code = next((code for code in ("METRICS_ACCESS_DENIED", "METRICS_INVALID_REQUEST") if code in text),
                "METRICS_UNAVAILABLE")
            raise MetricsServiceError(code)
        value = result.structured_content
        if request_tokens([value]) > self.max_tokens:
            raise ValueError("指标返回超过预算")
        model = DefinitionPage if name == "list_metric_definitions" else MetricPage
        page = model.model_validate(value)
        mismatch = page.model_id != request.model_id if isinstance(page, DefinitionPage) else page.request != request
        if mismatch:
            raise ValueError("指标返回与请求不一致")
        return page


@asynccontextmanager
async def connect_metrics(url, token, *, timeout=15):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    if not url.startswith("https://") or not token:
        raise ValueError("指标MCP必须配置HTTPS地址和面向服务的凭证")
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=timeout) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                client = MetricsClient(session)
                await client.discover()
                yield client


def build_metrics_server(*, authenticate, source):
    """source实现受权目录、精确窗口查询及稳定游标；身份与数据源均必须显式注入。"""
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.mcpserver.exceptions import ToolError
    if not callable(authenticate) or source is None:
        raise ValueError("必须显式配置身份验证与数据源")
    server = MCPServer("riskmind-metrics")

    @server.tool()
    async def list_metric_definitions(request: DefinitionRequest, ctx: Context) -> DefinitionPage:
        """读取当前用户可访问的指标口径、分组及统计窗口。"""
        try:
            actor = await authenticate(ctx)
            page = DefinitionPage.model_validate(await source.definitions(actor, request))
            if page.model_id != request.model_id or any(d.model_id != request.model_id
                or (request.metric is not None and d.metric != request.metric) for d in page.items):
                raise ValueError("目录范围错误")
            return page
        except PermissionError:
            raise ToolError("METRICS_ACCESS_DENIED") from None
        except ValueError:
            raise ToolError("METRICS_INVALID_REQUEST") from None
        except Exception:
            raise ToolError("METRICS_UNAVAILABLE") from None

    @server.tool()
    async def query_model_metrics(request: MetricQuery, ctx: Context) -> MetricPage:
        """读取已经计算保存的正式指标，不现场计算或聚合。"""
        try:
            actor = await authenticate(ctx)
            page = MetricPage.model_validate(await source.query(actor, request))
            if page.request != request:
                raise ValueError("数据源返回请求不一致")
            return page
        except PermissionError:
            raise ToolError("METRICS_ACCESS_DENIED") from None
        except ValueError:
            raise ToolError("METRICS_INVALID_REQUEST") from None
        except Exception:
            raise ToolError("METRICS_UNAVAILABLE") from None

    return server


@dataclass(frozen=True)
class MetricsTool:
    actor_id: str
    model_id: str
    client: MetricsClient
    name: ClassVar[str] = "query_model_metrics"

    def validate_binding(self, actor_id, resource):
        if actor_id != self.actor_id or resource.model_id != self.model_id:
            raise PermissionError("指标绑定不属于当前用户或模型")


def binding(runtime):
    return next(tool for tool in runtime.tools if isinstance(tool, MetricsTool))


def execution_limits(runtime):
    return {"model_id": binding(runtime).model_id, "protocol": "metrics/v1"}


def validate_request(runtime, request):
    if request.model_id != binding(runtime).model_id:
        raise PermissionError("模型超出指标授权范围")


def validate_result(request, limits, payload):
    if payload.request != request or request.model_id != limits["model_id"]:
        raise ValueError("指标结果、请求与授权范围不一致")


def observation_items(payload):
    return [{"reference_id": item.reference_id, "content": dict(item.model_dump(mode="json"),
        metric=payload.request.metric, definition_version=payload.request.definition_version,
        data_kind=payload.data_kind, result_set_version=payload.result_set_version)} for item in payload.items]


def validate_reference(reference, payload):
    ref = MetricReference.model_validate(reference)
    if ref.reference_id not in {item.reference_id for item in payload.items}:
        raise ValueError("引用不属于指标结果")


def authorize_result(runtime, result):
    if result.payload is None:
        binding(runtime)
        return True
    payload = MetricPage.model_validate(result.payload)
    validate_request(runtime, payload.request)
    return True


async def query_model_metrics(config: RunnableConfig, **arguments):
    runtime, task = config["configurable"]["runtime"], config["configurable"]["task"]
    request = MetricQuery.model_validate(arguments)
    validate_request(runtime, request)
    started = datetime.now(timezone.utc)
    try:
        async with asyncio.timeout(runtime.timeout_seconds):
            page = await binding(runtime).client.call("query_model_metrics", request)
        payload, error, status = page.model_dump(mode="json"), None, "SUCCEEDED"
    except (TimeoutError, RuntimeError, ValueError) as failure:
        payload, status = None, "FAILED"
        code = (failure.code if isinstance(failure, MetricsServiceError) else
            "METRICS_TIMEOUT" if isinstance(failure, TimeoutError) else
            "METRICS_RESULT_INVALID" if isinstance(failure, ValueError) else "METRICS_QUERY_FAILED")
        if code == "METRICS_ACCESS_DENIED":
            status = "REJECTED"
        error = ExecutionError(category="AUTHORIZATION" if status == "REJECTED" else "DEPENDENCY", code=code,
            message="指标查询失败，未取得可信结果", retryable=False)
    return TaskExecutionResult(execution_id="exec-"+uuid4().hex, task_id=task.task_id,
        capability=CapabilityType.FACT_RETRIEVAL, tool_name="query_model_metrics", result_version="1",
        status=status, started_at=started, completed_at=datetime.now(timezone.utc), payload=payload, error=error)
