"""真实MCP与账本组合；仅注入显式标记的测试指标和身份。"""
import asyncio
import json
import pytest
from sqlalchemy import select
from mcp import Client
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import resources
from test_registered_tool_postgres import Agent as BaseAgent
from test_metrics_mcp import Source, query
from risk_platform.execution.metrics import build_metrics_server, MetricsClient, MetricsTool
from risk_platform.execution.metrics import MetricsServiceError
from risk_platform.execution.metric_reads import QueryReadRow
from risk_platform.execution.tools import ToolCall
from risk_platform.workflow.contracts import InitialPlanningInput, UserQuery, InvestigationTask, InvestigationPlan
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.access import load_runtime_context
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord, AcceptedPlan
from risk_platform.workflow.observation import ObservationDraft
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy
from test_recovery_postgres import ProcessLost


class Agent(BaseAgent):
    interrupt = False
    def formulate_tool_call(self, *args, definition_messages, **kwargs):
        self.events.append("model")
        if not definition_messages:
            return dict(id="definition-1", name="list_metric_definitions", args={"model_id":"M"})
        assert json.loads(definition_messages[-1]["content"])["data_kind"] == "TEST"
        if self.interrupt:
            self.interrupt = False
            raise ProcessLost("指标定义已保存，模型返回前进程退出")
        return ToolCall("query_model_metrics", query())

    def observe_round(self, *args, **kwargs):
        return ObservationDraft(findings=(), unresolved_questions=(), action="STOP", stop_reason="测试资料不作生产结论")


@pytest.mark.parametrize("interrupt", [False, True])
def test_metrics_directory_commit_query_result_and_checkpoint_reuse(database, interrupt):
    async def scenario():
        source = Source()
        async def authenticate(ctx):
            return "alice"
        agent = Agent([])
        agent.interrupt = interrupt
        planning = InitialPlanningInput(user_query=UserQuery(query="查询样本量", model_id="M"),
            available_capabilities=["FACT_RETRIEVAL"])
        async with Client(build_metrics_server(authenticate=authenticate, source=source)) as session:
            client = MetricsClient(session)
            await client.discover()
            async def runtime(sessions):
                async with sessions() as s:
                    return await load_runtime_context(s, investigation_id="I", round_id="R", actor_id="A",
                        authorize_resource=lambda *_:True, tools=(MetricsTool("A", "M", client),),
                        action="investigation:continue")
            async with resources(database, agent, setup=True) as (sessions, saver, runner):
                async with sessions.begin() as s:
                    await ledger.register_investigation(s, InvestigationRecord(investigation_id="I", created_by="A", planning_input=planning))
                    await ledger.register_round(s, RoundRecord(investigation_id="I", round_id="R", round_number=1))
                    await ledger.accept_plan(s, AcceptedPlan(investigation_id="I", round_id="R", plan_id="P", plan_version=1,
                        plan=InvestigationPlan(goal="查询指标", tasks=[InvestigationTask(task_id="T", objective="查询样本量",
                            expected_result="带口径与来源的数值", capability="FACT_RETRIEVAL")])))
                if interrupt:
                    with pytest.raises(ProcessLost):
                        await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
                first = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)),
                    policy=RecoveryPolicy(retry_uncertain=True))
                assert first["task_execution_results"][0].status == "SUCCEEDED"
                async with sessions() as s:
                    assert len(list(await s.scalars(select(QueryReadRow)))) == 1
            before = list(agent.events)
            count = len(source.calls)
            async with resources(database, agent) as (sessions, saver, runner):
                second = await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
                assert second == first and agent.events == before and len(source.calls) == count
                source.allowed = False
                with pytest.raises(MetricsServiceError):
                    await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await runtime(sessions)))
    asyncio.run(scenario())
