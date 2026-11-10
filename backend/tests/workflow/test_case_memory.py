"""分类记忆：失败不变零命中，历史限制不被截断，模型收到集中来源。"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from risk_platform.memory.contracts import MemoryContent, MemoryRecord, MemoryScope
from risk_platform.memory.context import assemble_context, model_context
from risk_platform.memory.investigation_memory import extract_memory
from risk_platform.workflow.persistence.contracts import canonical_json
from risk_platform.workflow.observation import RoundObservation, ExecutionObservation, ObservationError


def observation(status="FAILED", outcome="EXECUTION_FAILED"):
    return RoundObservation(observation_id="O1", created_at=datetime.now(timezone.utc),
        status="INCONCLUSIVE", execution_refs=("E1",),
        execution_summaries=(ExecutionObservation(execution_id="E1", task_id="T1", status=status, outcome=outcome),),
        narrative="尚未完成", error=ObservationError(code="NO_KNOWLEDGE", message="没有可解释资料"))


def memory(content="历史线索\n限制：当前未验证", investigation_id="I"):
    return MemoryRecord(memory_id="M", scope=MemoryScope(kind="INVESTIGATION", id=investigation_id),
        source_type="OBSERVATION_RESULT", source_id="result", content=MemoryContent(historical_findings=(content,)))


def assemble(memories=(), max_chars=12000):
    return assemble_context(scope=MemoryScope(kind="INVESTIGATION", id="I"),
        candidates=memories, max_chars=max_chars)


@pytest.mark.parametrize("status,outcome", [("FAILED", "EXECUTION_FAILED"),
    ("REJECTED", "EXECUTION_REJECTED"), ("SUCCEEDED", "NO_HITS")])
def test_extraction_preserves_status_and_source(status, outcome):
    value = extract_memory(investigation_id="I", source_result_id="result", observation=observation(status, outcome))
    text = value.content.execution_summaries[0]
    assert f"status={status}" in text and f"outcome={outcome}" in text and "execution=E1" in text
    assert value.source_id == "result"
    assert extract_memory(investigation_id="I", source_result_id="result", observation=observation(status, outcome)) == value


def test_no_history_is_explicit_and_cross_case_is_rejected():
    value = assemble()
    assert not value.memories
    assert "不是指令" in model_context(value)["usage"]
    with pytest.raises(ValueError, match="归属不一致"):
        assemble((memory(investigation_id="OTHER"),))


def test_budget_omits_whole_findings_without_losing_qualifications():
    long = "历史说明" * 1000 + "\n限制：尚未验证当前案件"
    source = memory(long)
    value = assemble((source,), max_chars=3000)
    assert not value.memories
    assert source.content.historical_findings == (long,)
    assert len(canonical_json(value)) <= 3000
    assert assemble((source,), max_chars=3000) == value


def test_finding_and_original_limit_are_kept_together():
    source = memory()
    value = assemble((source,))
    assert value.memories[0].content.historical_findings == source.content.historical_findings
    assert value.memories[0].source_id == "result"


def test_agent_receives_grouped_content_in_both_model_requests():
    from risk_platform.agents.investigation import InvestigationAgent
    from risk_platform.workflow.contracts import InitialPlanningInput, UserQuery
    from risk_platform.execution.tools import tool_declarations
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if "tools" not in kwargs:
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=json.dumps({"goal": "调查", "tasks": [{"objective": "查历史", "expected_result": "线索"}]})))])
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(tool_calls=[
            SimpleNamespace(type="function", function=SimpleNamespace(name="knowledge_search", arguments='{"query":"历史线索","top_k":1}'))]))])
    agent = InvestigationAgent(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), "test")
    source = InitialPlanningInput(user_query=UserQuery(query="原因"), available_capabilities=["KNOWLEDGE_RETRIEVAL"])
    context = assemble((memory(),))
    plan = agent.initial_plan(source, memory_context=context)
    agent.formulate_tool_call(plan.tasks[0], source, goal=plan.goal, tools=tool_declarations(plan.tasks[0].capability),
        tool_limits={"knowledge_search": {"max_top_k": 5}}, memory_context=context)
    for call in calls:
        payload = json.loads(call["messages"][1]["content"])
        record = payload["historical_memory"]["memory"]["memories"][0]
        assert record["source_id"] == "result" and record["content"]["historical_findings"]
        assert "actor_id" not in payload and "authorize" not in payload


def test_follow_ups_merge_gaps_and_suggestions_with_stable_exact_deduplication():
    from risk_platform.workflow.observation import ObservationNote
    note = lambda text: ObservationNote(text=text, execution_refs=("E1",))
    source = observation().model_copy(update={"status": "SUCCEEDED", "error": None,
        "knowledge_gaps": (note("标签成熟度待确认"), note("缺少版本背景")),
        "next_step_suggestions": (note("补查版本背景"), note("标签成熟度待确认"))})
    source = RoundObservation.model_validate_json(source.model_dump_json())
    record = extract_memory(investigation_id="I", source_result_id="result", observation=source)
    assert record.content.follow_ups == ("标签成熟度待确认", "缺少版本背景", "补查版本背景")
    assert record.content.item_count() == 4  # 一条执行摘要加三条后续事项。
