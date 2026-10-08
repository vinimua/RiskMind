from risk_platform.workflow.graph import build_investigation_graph


def test_investigation_graph_compiles() -> None:
    graph = build_investigation_graph()

    assert graph is not None


def test_investigation_graph_runs_to_v0_completion() -> None:
    graph = build_investigation_graph()
    query = "Investigate a model monitoring anomaly"

    result = graph.invoke({"query": query})

    assert result == {
        "query": query,
        "phase": "REPLANNING",
        "investigation_complete": True,
    }
