"""显式工具声明与执行分派，不把注册等同于授权。"""

import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest

from risk_platform.execution import knowledge, tools
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.workflow.contracts import CapabilityType
from test_first_round_execution import state, context
from test_observation import execution


def test_registered_schema_has_no_identity_and_cannot_be_mutated_by_caller():
    declarations = tools.tool_declarations(CapabilityType.KNOWLEDGE_RETRIEVAL)
    assert declarations[0]["function"]["name"] == KnowledgeSearchTool.name
    schema = declarations[0]["function"]["parameters"]
    assert set(schema["properties"]) == {"query", "top_k", "filters"}
    assert schema["additionalProperties"] is False
    declarations[0]["function"]["name"] = "forged"
    assert tools.tool_declarations(CapabilityType.KNOWLEDGE_RETRIEVAL)[0]["function"]["name"] == "knowledge_search"
    with pytest.raises(TypeError):
        tools.TOOL_DEFINITIONS["forged"] = tools.KNOWLEDGE_SEARCH


def test_no_declaration_for_unimplemented_capability():
    with pytest.raises(ValueError):
        tools.tool_declarations(CapabilityType.COMPUTATION)


@pytest.mark.parametrize("name,capability,code", [
    ("shell_execute", "KNOWLEDGE_RETRIEVAL", "UNKNOWN_TOOL"),
    ("knowledge_search", "COMPUTATION", "UNSUPPORTED_CAPABILITY"),
])
def test_unregistered_or_mismatched_tool_never_calls_handler(monkeypatch, name, capability, code):
    async def forbidden(*_, **__):
        pytest.fail("未知或错能力工具不能进入 handler")
    monkeypatch.setattr(knowledge, "execute_knowledge_search", forbidden)
    task = state(1)["investigation_plan"].tasks[0].model_copy(update={"capability": CapabilityType(capability)})
    result = asyncio.run(tools.execute_registered_tool(name, task, {"query": "q", "top_k": 1}))
    assert result.status == "REJECTED" and result.error.code == code and result.payload is None


def test_registered_dispatch_delegates_to_existing_adapter(monkeypatch):
    expected, seen = execution(empty=True), []
    async def adapter(task, request, **options):
        seen.append((task, request, options))
        return expected
    monkeypatch.setattr(knowledge, "execute_knowledge_search", adapter)
    task, request, trusted = state(1)["investigation_plan"].tasks[0], {"query": "q", "top_k": 1}, context()
    result = asyncio.run(tools.execute_registered_tool("knowledge_search", task, request, context=trusted))
    assert result is expected and seen == [(task, request, {"context": trusted})]


def test_registration_does_not_bypass_authorization(monkeypatch):
    async def forbidden(*_, **__):
        pytest.fail("权限拒绝不能检索")
    monkeypatch.setattr(knowledge, "retrieve_dense_children", forbidden)
    result = asyncio.run(tools.execute_registered_tool("knowledge_search", state(1)["investigation_plan"].tasks[0],
        {"query": "q", "top_k": 1}, context=context(), authorize=lambda *_: False))
    assert result.status == "REJECTED" and result.error.code == "TOOL_NOT_AUTHORIZED"


def test_contract_digest_includes_tool_schema_and_description(monkeypatch):
    original = tools.query_contract_digest("prompt")
    modified = replace(tools.KNOWLEDGE_SEARCH, description="不同工具说明")
    monkeypatch.setattr(tools, "TOOL_DEFINITIONS", MappingProxyType({modified.name: modified}))
    assert tools.query_contract_digest("prompt") != original
