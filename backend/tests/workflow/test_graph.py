from risk_platform.workflow import nodes
from risk_platform.workflow.graph import build_investigation_graph


INVESTIGATION_NEEDS = [
    "检查标签成熟度",
    "检查模型或特征版本变化",
    "检查客群结构变化",
]


def test_initial_planning_delegates_to_investigation_agent(monkeypatch) -> None:
    received_queries: list[str] = []

    def fake_initial_plan(query: str) -> list[str]:
        received_queries.append(query)
        return INVESTIGATION_NEEDS

    monkeypatch.setattr(nodes.investigation_agent, "initial_plan", fake_initial_plan)
    query = "Investigate a model monitoring anomaly"

    result = nodes.initial_planning({"query": query})

    assert received_queries == [query]
    assert result["investigation_needs"] == INVESTIGATION_NEEDS


def test_investigation_graph_compiles() -> None:
    graph = build_investigation_graph()

    assert graph is not None


def test_investigation_graph_runs_to_v0_completion(monkeypatch) -> None:
    monkeypatch.setattr(
        nodes.investigation_agent,
        "initial_plan",
        lambda query: INVESTIGATION_NEEDS,
    )
    graph = build_investigation_graph()
    query = "Investigate a model monitoring anomaly"

    result = graph.invoke({"query": query})

    assert result == {
        "query": query,
        "investigation_needs": INVESTIGATION_NEEDS,
        "investigation_complete": True,
    }
