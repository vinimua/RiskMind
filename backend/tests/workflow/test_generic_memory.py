"""共用分类正文、集中来源、预算与恢复完整性。"""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from risk_platform.memory.contracts import MemoryContent, MemoryRecord, MemoryScope, AgentContext, ContextSnapshot
from risk_platform.memory.context import assemble_context
from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
from risk_platform.memory.repository import read_context_snapshot, read_memory, _is_projection
from risk_platform.workflow.persistence.repository import LedgerIntegrityError


def chat(content=None, conversation="C"):
    return MemoryRecord(memory_id="M", scope=MemoryScope(kind="CONVERSATION", id=conversation),
        source_type="CHAT_MESSAGE", source_id="message-1", content=content or MemoryContent(user_preferences=("请简洁回答",)))


def assemble(records, **kwargs):
    return assemble_context(scope=MemoryScope(kind="CONVERSATION", id="C"), candidates=records, **kwargs)


def test_chat_has_grouped_strings_and_one_source_without_case_fields():
    record = chat(MemoryContent(user_preferences=("回答简洁",), follow_ups=("补测试",), background=("使用 PostgreSQL",)))
    value = assemble((record,))
    assert value.memories == (record,)
    payload = canonical_json(value)
    assert payload.count('"source_id"') == 1
    for field in ("round_id", "observation_id", "execution_refs", "item_index", "limitation"):
        assert field not in payload
    assert AgentContext.model_validate_json(value.model_dump_json()) == value


def test_budget_keeps_short_items_and_omits_long_item_whole():
    record = chat(MemoryContent(user_preferences=("长文"*3000, "简洁回答")))
    value = assemble((record,), max_chars=2500)
    assert value.memories[0].content.user_preferences == ("简洁回答",)
    assert len(canonical_json(value)) <= 2500
    assert len(record.content.user_preferences[0]) == 6000


@pytest.mark.parametrize("scope", [MemoryScope(kind="CONVERSATION", id="OTHER"), MemoryScope(kind="INVESTIGATION", id="I")])
def test_cross_scope_rejected_before_budget_omission(scope):
    with pytest.raises(ValidationError, match="归属不一致"):
        assemble_context(scope=scope, candidates=(chat(),), max_chars=512)


def test_source_and_content_constraints():
    with pytest.raises(ValidationError, match="来源类型"):
        chat().model_validate({**chat().model_dump(), "source_type": "OBSERVATION_RESULT"})
    with pytest.raises(ValidationError, match="不能冒充"):
        chat(MemoryContent(execution_summaries=("工具成功",)))
    with pytest.raises(ValidationError, match="包含内容"):
        MemoryRecord(memory_id="M", scope=MemoryScope(kind="CONVERSATION", id="C"), source_type="CHAT_MESSAGE", source_id="msg", content=MemoryContent())
    with pytest.raises(ValidationError):
        MemoryContent(user_preferences=("   ",))


def test_duplicate_records_are_checked():
    with pytest.raises(ValidationError, match="重复"):
        assemble((chat(), chat()))


def test_projection_can_only_omit_whole_text_not_rewrite_reorder_or_duplicate():
    original = chat(MemoryContent(follow_ups=("A", "B", "C")))
    def projection(values):
        return original.model_copy(update={"content": MemoryContent(follow_ups=values)})
    assert _is_projection(projection(("A", "C")), original)
    for values in (("C", "A"), ("A", "A"), ("changed",), ("A-prefix",)):
        assert not _is_projection(projection(values), original)
    assert not _is_projection(original.model_copy(update={"source_id": "OTHER"}), original)


def test_current_readers_refuse_old_formats_before_parsing_and_check_digest():
    for version in (1, 2, 3, 4, 5, 7):
        with pytest.raises(LedgerIntegrityError, match="旧版"):
            read_context_snapshot(SimpleNamespace(schema_version=version, payload="invalid-json"))
    with pytest.raises(LedgerIntegrityError, match="旧版"):
        read_memory(SimpleNamespace(schema_version=1, payload="invalid-json"))
    snapshot = ContextSnapshot(run_id="call", stage="chat", context=assemble((chat(),)))
    row = SimpleNamespace(schema_version=6, payload=canonical_json(snapshot), input_digest=input_digest(snapshot))
    assert read_context_snapshot(row) == snapshot
    row.input_digest = "0"*64
    with pytest.raises(LedgerIntegrityError, match="摘要"):
        read_context_snapshot(row)


def test_generic_content_and_assembler_do_not_import_workflow_or_execution():
    from risk_platform.memory import contracts, context
    for module in (contracts, context):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("risk_platform.workflow", "risk_platform.execution"))


@pytest.mark.parametrize("old_field", ["tasks", "open_questions", "decisions"])
def test_retired_categories_are_rejected(old_field):
    with pytest.raises(ValidationError):
        MemoryContent.model_validate({old_field: ["旧内容"]})


def test_context_has_no_purpose_field_and_rejects_the_retired_argument():
    context = assemble((chat(),))
    assert "purpose" not in context.model_dump()
    with pytest.raises(ValidationError):
        AgentContext.model_validate({**context.model_dump(), "purpose": "CHAT_RESPONSE"})


@pytest.mark.parametrize("field", ["omitted_count", "truncated"])
def test_context_does_not_store_redundant_coverage_fields(field):
    context = assemble((chat(),))
    assert field not in context.model_dump()
    with pytest.raises(ValidationError):
        AgentContext.model_validate({**context.model_dump(), field: 0})
