"""knowledge_search 的可信绑定：工具专用范围和权限不污染案件级 Runtime。

请求与执行仍复用 execution.knowledge 的 7A 契约、校验和执行入口。
这里只负责把应用授予的绑定适配给该入口，不增加第二套工具执行流程。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.workflow.runtime import (
    InvestigationAccessError, InvestigationAction, InvestigationResource, InvestigationRuntimeContext, check_runtime,
)


@dataclass(frozen=True)
class KnowledgeSearchTool:
    name: ClassVar[str] = "knowledge_search"
    execution: KnowledgeExecutionContext
    authorize: Callable[[str, str, KnowledgeExecutionContext], bool]
    allow_unscoped: bool = False

    def validate_binding(self, actor_id: str, resource: InvestigationResource) -> None:
        """知识工具必须绑定同一 actor 和案件模型；无模型范围必须显式许可。"""
        execution = KnowledgeExecutionContext.model_validate_json(self.execution.model_dump_json())
        if execution.actor_id != actor_id:
            raise InvestigationAccessError("知识工具身份与运行身份不一致")
        if resource.model_id is not None:
            if execution.scope.model_id != resource.model_id:
                raise InvestigationAccessError("调查目标与知识工具 model_id 范围必须一致")
        elif self.allow_unscoped is not True:
            raise InvestigationAccessError("无模型目标的知识查询必须显式授权范围")
        if not callable(self.authorize):
            raise InvestigationAccessError("知识工具必须配置授权函数")


def knowledge_tool(context: InvestigationRuntimeContext) -> KnowledgeSearchTool:
    """缺少绑定就拒绝；不使用默认工具、默认 profile 或模型提供的配置。"""
    for tool in context.tools:
        if tool.name == KnowledgeSearchTool.name and type(tool) is KnowledgeSearchTool:
            return tool
    raise InvestigationAccessError("当前运行未授予 knowledge_search 工具")


def knowledge_execution(context: InvestigationRuntimeContext, action: InvestigationAction,
                        planning_input=None) -> KnowledgeExecutionContext:
    check_runtime(context, action, planning_input)
    tool = knowledge_tool(context)
    execution = KnowledgeExecutionContext.model_validate_json(tool.execution.model_dump_json())
    # 模型阶段使用通用运行预算；工具执行不能超过运行预算，保留更严格的工具限制。
    return execution.model_copy(update={"timeout_seconds": min(execution.timeout_seconds, context.timeout_seconds)})


def knowledge_authorizer(context: InvestigationRuntimeContext):
    """每次工具调用重新核对调查权限，再调用工具自己的授权函数。"""
    def authorize(actor, action, execution):
        try:
            trusted = knowledge_execution(context, InvestigationAction.CONTINUE)
            if actor != trusted.actor_id or execution != trusted:
                return False
            return knowledge_tool(context).authorize(actor, action, execution) is True
        except Exception:
            return False
    return authorize
