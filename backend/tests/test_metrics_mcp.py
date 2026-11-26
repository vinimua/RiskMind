"""真实MCP协议、测试数据源；不表示已经接入生产指标或身份系统。"""
import asyncio
from datetime import datetime, timezone
import pytest
from mcp import Client
from risk_platform.execution.metrics import (
    build_metrics_server, MetricsClient, DefinitionRequest, DefinitionPage, MetricDefinition,
    MetricQuery, MetricPage, MetricItem, observation_items, validate_reference,
)


def query(metric="sample_count", cursor=None):
    return MetricQuery(model_id="M", metric=metric, definition_version="1",
        start_date="2026-10-01", end_date="2026-11-01", group_by="channel", cursor=cursor)


class Source:
    def __init__(self):
        self.allowed = True
        self.calls = []

    def authorize(self, actor, model):
        if not self.allowed or actor != "alice" or model != "M":
            raise PermissionError("数据不可访问")

    async def definitions(self, actor, request):
        self.authorize(actor, request.model_id)
        return DefinitionPage(model_id="M", data_kind="TEST", items=tuple(
            MetricDefinition(model_id="M", metric=name, version="1", description=name,
                groups=("channel",), windows=(("2026-10-01", "2026-11-01"),))
            for name in ("sample_count", "missing_rate") if request.metric in (None, name)))

    async def query(self, actor, request):
        self.authorize(actor, request.model_id)
        self.calls.append(request)
        if request.definition_version != "1" or request.group_by != "channel":
            raise ValueError("口径或分组失效")
        if request.start_date.isoformat() != "2026-10-01":
            return MetricPage(request=request, status="NO_DATA", data_kind="TEST", result_set_version="S1", items=())
        if request.cursor not in (None, "S1-page2"):
            raise ValueError("游标不属于固定结果集")
        return MetricPage(request=request, status="AVAILABLE", data_kind="TEST", result_set_version="S1",
            items=(MetricItem(reference_id="row-2" if request.cursor else "row-1", model_version="v1",
                group="branch" if request.cursor else "mobile", value=100 if request.metric == "sample_count" else .1,
                sample_count=100, computed_at=datetime.now(timezone.utc),
                details={} if request.metric == "sample_count" else {"missing_count":10}),),
            next_cursor=None if request.cursor else "S1-page2")


@pytest.mark.parametrize("metric", ["sample_count", "missing_rate"])
def test_real_protocol_discovery_queries_pages_and_references(metric):
    async def scenario():
        source = Source()
        async def authenticate(ctx):
            return "alice"
        server = build_metrics_server(authenticate=authenticate, source=source)
        async with Client(server) as session:
            client = MetricsClient(session)
            await client.discover()
            catalogue = await client.call("list_metric_definitions", DefinitionRequest(model_id="M", metric=metric))
            assert len(catalogue.items) == 1 and catalogue.data_kind == "TEST"
            page = await client.call("query_model_metrics", query(metric))
            following = await client.call("query_model_metrics", query(metric, page.next_cursor))
            assert page.result_set_version == following.result_set_version
            assert page.items[0].reference_id != following.items[0].reference_id
            assert observation_items(page)[0]["content"]["data_kind"] == "TEST"
            validate_reference({"reference_id":"row-1"}, page)
            with pytest.raises(ValueError):
                validate_reference({"reference_id":"other"}, page)
            source.allowed = False
            with pytest.raises(RuntimeError):
                await client.call("query_model_metrics", query(metric))
    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_protocol_rejects_identity_spoofing_bad_version_and_unknown_tool():
    async def scenario():
        source = Source()
        async def authenticate(ctx):
            return "alice"
        async with Client(build_metrics_server(authenticate=authenticate, source=source)) as session:
            client = MetricsClient(session)
            with pytest.raises(ValueError):
                MetricQuery.model_validate(query().model_dump() | {"actor_id":"admin"})
            with pytest.raises(RuntimeError):
                await client.call("query_model_metrics", query().model_copy(update={"definition_version":"2"}))
            with pytest.raises(RuntimeError):
                await client.call("query_model_metrics", query().model_copy(update={"model_id":"other"}))
            with pytest.raises(ValueError):
                await client.call("execute_sql", query())
    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_window_and_atomic_result_contracts():
    with pytest.raises(ValueError):
        MetricQuery.model_validate(query().model_dump() | {"end_date":"2026-10-01"})
    with pytest.raises(ValueError):
        MetricPage(request=query(), data_kind="TEST", status="AVAILABLE", result_set_version="S1", items=())


def test_model_is_offered_definition_reader_and_returns_structured_read_intent():
    from risk_platform.agents.investigation import InvestigationAgent
    from risk_platform.execution.tools import MODEL_METRICS
    from risk_platform.workflow.contracts import InvestigationTask, InitialPlanningInput, UserQuery
    from tool_model_helpers import ToolModelStub, tool_response
    model = ToolModelStub(tool_response("list_metric_definitions", {"model_id":"M"}))
    agent = InvestigationAgent(client=None, model="test", tool_model=model)
    call = agent.formulate_tool_call(InvestigationTask(task_id="T", objective="查询样本量",
        expected_result="指标", capability="FACT_RETRIEVAL"),
        InitialPlanningInput(user_query=UserQuery(query="查指标", model_id="M"),
            available_capabilities=["FACT_RETRIEVAL"]), goal="查指标",
        tools=[MODEL_METRICS.tool], tool_limits={}, definition_messages=[])
    assert call["name"] == "list_metric_definitions"
    assert len(model.binding["tools"]) == 2
    assert model.binding["tools"][1]["function"]["name"] == "list_metric_definitions"
