"""通用身份环境与知识工具绑定分别验证，不能靠模型或 State 扩权。"""

from dataclasses import replace

import pytest

from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool, knowledge_execution, knowledge_authorizer
from risk_platform.workflow.runtime import InvestigationAction, InvestigationAccessError, check_runtime
from test_runtime import make_runtime, planning


def test_generic_runtime_needs_no_knowledge_tool():
    context = replace(make_runtime(planning()), tools=())
    assert check_runtime(context, InvestigationAction.READ) is context
    assert not hasattr(context, "execution")
    assert not hasattr(context, "authorize_knowledge")
    with pytest.raises(InvestigationAccessError, match="未授予"):
        knowledge_execution(context, InvestigationAction.CONTINUE)


def test_generic_tool_binding_does_not_require_knowledge_fields():
    class ReadOnlyTool:
        name = "test_only_read"

        def validate_binding(self, actor_id, resource):
            assert actor_id == "actor" and resource.model_id == "M"

    context = replace(make_runtime(planning()), tools=(ReadOnlyTool(),))
    check_runtime(context, InvestigationAction.CONTINUE)
    # 相同环境可以安装其他可信绑定，但不会隐式得到知识能力。
    with pytest.raises(InvestigationAccessError, match="未授予"):
        knowledge_execution(context, InvestigationAction.CONTINUE)


@pytest.mark.parametrize("timeout", [True, 0, -1, float("nan"), float("inf"), "30"])
def test_invalid_generic_timeout_is_rejected(timeout):
    with pytest.raises(InvestigationAccessError):
        check_runtime(replace(make_runtime(planning()), timeout_seconds=timeout), InvestigationAction.READ)


def test_tool_actor_cannot_differ_from_run_actor():
    with pytest.raises(InvestigationAccessError):
        check_runtime(replace(make_runtime(planning()), actor_id="other"), InvestigationAction.READ)


@pytest.mark.parametrize("tools", [lambda tool: [tool], lambda tool: (tool, tool), lambda tool: ({"name": "knowledge_search"},)])
def test_tool_bindings_are_explicit_unique_and_typed(tools):
    context = make_runtime(planning())
    with pytest.raises(InvestigationAccessError):
        check_runtime(replace(context, tools=tools(context.tools[0])), InvestigationAction.READ)


def test_budget_is_capped_without_mutating_original_grant():
    context = make_runtime(planning())
    limited = replace(context, timeout_seconds=1)
    execution = knowledge_execution(limited, InvestigationAction.CONTINUE)
    assert execution.timeout_seconds == 1
    assert context.tools[0].execution.timeout_seconds > 1
    assert knowledge_authorizer(limited)(execution.actor_id, "knowledge:search", execution) is True


def test_tool_permission_is_independent_and_rechecked():
    permission = {"allowed": True}
    context = make_runtime(planning(), authorize_knowledge=lambda *_: permission["allowed"])
    execution = knowledge_execution(context, InvestigationAction.CONTINUE)
    authorize = knowledge_authorizer(context)
    assert authorize(execution.actor_id, "knowledge:search", execution) is True
    permission["allowed"] = False
    assert authorize(execution.actor_id, "knowledge:search", execution) is False
    assert check_runtime(context, InvestigationAction.READ) is context


def test_impostor_cannot_use_reserved_knowledge_tool_name():
    class Impostor:
        name = KnowledgeSearchTool.name
        def validate_binding(self, *_):
            pass
    context = replace(make_runtime(planning()), tools=(Impostor(),))
    with pytest.raises(InvestigationAccessError, match="未授予"):
        knowledge_execution(context, InvestigationAction.CONTINUE)
