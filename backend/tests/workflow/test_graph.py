from uuid import UUID

from risk_platform.workflow import nodes
from risk_platform.workflow.contracts import (
    CapabilityType,
    InvestigationPlan,
    InvestigationTask,
    TaskStatus,
)
from risk_platform.workflow.graph import build_investigation_graph


def _investigation_plan() -> InvestigationPlan:
    return InvestigationPlan(
        goal="调查 credit_v3 KS 下降原因",
        hypotheses=["标签成熟度不足", "客群结构变化", "特征漂移"],
        tasks=[
            InvestigationTask(
                objective="确认当前评估样本标签是否成熟",
                capability=CapabilityType.COMPUTATION,
                expected_result="得到当前标签成熟状态",
            ),
            InvestigationTask(
                objective="确认近期是否发生模型或特征版本变化",
                capability=CapabilityType.FACT_RETRIEVAL,
                expected_result="得到当前版本和近期变更记录",
            ),
            InvestigationTask(
                objective="搜索历史类似 KS 下降案例",
                capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                expected_result="得到相关历史 Case 和已有调查经验",
            ),
        ],
    )


def test_investigation_task_defaults_are_system_managed() -> None:
    task = _investigation_plan().tasks[0]

    assert str(UUID(task.task_id)) == task.task_id
    assert task.status is TaskStatus.PENDING


def test_capability_types_round_trip_through_json() -> None:
    for capability in CapabilityType:
        task = InvestigationTask(
            objective="测试调查任务",
            capability=capability,
            expected_result="得到测试结果",
        )

        restored = InvestigationTask.model_validate_json(task.model_dump_json())

        assert restored.capability is capability


def test_initial_planning_delegates_to_investigation_agent(monkeypatch) -> None:
    received_queries: list[str] = []
    plan = _investigation_plan()

    def fake_initial_plan(query: str) -> InvestigationPlan:
        received_queries.append(query)
        return plan

    monkeypatch.setattr(nodes.investigation_agent, "initial_plan", fake_initial_plan)
    query = "Investigate a model monitoring anomaly"

    result = nodes.initial_planning({"query": query})

    assert received_queries == [query]
    assert result["investigation_plan"] == plan


def test_investigation_graph_compiles() -> None:
    graph = build_investigation_graph()

    assert graph is not None


def test_investigation_graph_runs_to_v0_completion(monkeypatch) -> None:
    plan = _investigation_plan()
    monkeypatch.setattr(
        nodes.investigation_agent,
        "initial_plan",
        lambda query: plan,
    )
    graph = build_investigation_graph()
    query = "Investigate a model monitoring anomaly"

    result = graph.invoke({"query": query})

    assert result == {
        "query": query,
        "investigation_plan": plan,
        "investigation_complete": True,
    }
