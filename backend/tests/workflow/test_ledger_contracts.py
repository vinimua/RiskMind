"""8A 的类型与 canonical 摘要；数据库 invariant 另在真实 PG 验证。"""

import json

import pytest
from pydantic import BaseModel, ValidationError

from risk_platform.execution.contracts import KnowledgeExecutionContext, KnowledgeSearchToolRequest
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, InvestigationTask, UserQuery
from risk_platform.workflow.persistence.contracts import (
    AcceptedPlan, CommittedArtifact, InvestigationRecord, OperationInput, OperationType,
    RoundRecord, canonical_json, input_digest, operation_key,
)


def investigation():
    return InvestigationRecord(investigation_id="I", created_by="actor",
        planning_input=InitialPlanningInput(user_query=UserQuery(query="为什么指标下降？"),
                                            available_capabilities=["KNOWLEDGE_RETRIEVAL"]))


def accepted_plan():
    return AcceptedPlan(investigation_id="I", round_id="R", plan_id="P", plan_version=1,
                        plan=InvestigationPlan(goal="调查历史线索", tasks=[InvestigationTask(
                            task_id="T0", objective="查询历史", capability="KNOWLEDGE_RETRIEVAL", expected_result="历史线索")]))


def operation(kind=OperationType.KNOWLEDGE_EXECUTION, **changes):
    data = dict(investigation_id="I", round_id="R", plan_id="P", task_id="T0", operation_type=kind,
                context=KnowledgeExecutionContext(actor_id="actor", index_profile="parent-child-v1"))
    if kind == OperationType.KNOWLEDGE_EXECUTION:
        data["candidate_request"] = KnowledgeSearchToolRequest(query="历史案例", top_k=1)
    else:
        data.update(model_name="test-model", prompt_version="prompt-v1")
    if kind == OperationType.OBSERVATION:
        data.update(task_id=None, execution_refs=("E0",))
    data.update(changes)
    return OperationInput(**data)


def test_canonical_object_key_order_but_not_array_order():
    class Input(BaseModel):
        schema_version: int
        values: dict[str, list[int]]
    a = Input(schema_version=1, values={"中": [1, 2], "a": [3]})
    b = Input(schema_version=1, values={"a": [3], "中": [1, 2]})
    assert input_digest(a) == input_digest(b)
    assert "中" in canonical_json(a) and " " not in canonical_json(a)
    b.values["中"].reverse()
    assert input_digest(a) != input_digest(b)
    assert input_digest(a) != input_digest(a.model_copy(update={"schema_version": 2}))


@pytest.mark.parametrize("change", [
    {"candidate_request": KnowledgeSearchToolRequest(query="另一个问题", top_k=1)},
    {"context": KnowledgeExecutionContext(actor_id="other", index_profile="parent-child-v1")},
    {"context": KnowledgeExecutionContext(actor_id="actor", index_profile="baseline-v1")},
    {"context": KnowledgeExecutionContext(actor_id="actor", index_profile="parent-child-v1", scope={"model_id": "M"})},
])
def test_changed_business_input_same_key_different_digest(change):
    before, after = operation(), operation(**change)
    assert operation_key(before) == operation_key(after)
    assert input_digest(before) != input_digest(after)


def test_stage_contracts_and_frozen_extra_forbid():
    for value in (investigation(), RoundRecord(investigation_id="I", round_id="R", round_number=1), accepted_plan(), operation()):
        with pytest.raises(ValidationError):
            type(value).model_validate({**value.model_dump(), "secret": "forbidden"})
        with pytest.raises(ValidationError):
            value.schema_version = 2
    for changes in ({"schema_version": 2}, {"task_id": None}, {"candidate_request": None},
                    {"model_name": "not-execution-input"}, {"execution_refs": ("E0",)}):
        with pytest.raises(ValidationError):
            operation(**changes)
    assert operation(OperationType.QUERY_FORMULATION).candidate_request is None
    assert operation(OperationType.OBSERVATION).task_id is None
    with pytest.raises(ValidationError):
        CommittedArtifact()
    with pytest.raises(ValidationError):
        AcceptedPlan.model_validate({**accepted_plan().model_dump(), "plan": {"goal": "x", "tasks": []}})


def test_canonical_rejects_nonfinite_numbers_and_preserves_text():
    class Input(BaseModel):
        value: float
        text: str
    with pytest.raises(ValueError):
        canonical_json(Input(value=float("nan"), text="正文"))
    assert json.loads(canonical_json(Input(value=1, text=" 正文\n")))["text"] == " 正文\n"
