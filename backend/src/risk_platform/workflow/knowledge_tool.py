"""knowledge_search 的可信绑定：工具专用范围和权限不污染案件级 Runtime。

请求与执行仍复用 execution.knowledge 的 7A 契约、校验和执行入口。
这里只负责把应用授予的绑定适配给该入口，不增加第二套工具执行流程。
"""

from collections.abc import Callable
from dataclasses import dataclass, field
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
    # 运行依赖只属于工具；不进入请求、业务摘要、State 或 checkpoint。
    # 仅保存引用，不在构图/绑定时创建连接；生命周期由可信应用管理。
    session_factory: Callable | None = field(default=None, kw_only=True, repr=False, compare=False)
    embedding_client: object | None = field(default=None, kw_only=True, repr=False, compare=False)
    milvus_client: object | None = field(default=None, kw_only=True, repr=False, compare=False)

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

    def execution_context(self, timeout_seconds) -> KnowledgeExecutionContext:
        """纯配置提取：运行预算只收紧工具预算，不修改应用授予的原始配置。"""
        execution = KnowledgeExecutionContext.model_validate_json(self.execution.model_dump_json())
        return execution.model_copy(update={"timeout_seconds": min(execution.timeout_seconds, timeout_seconds)})

    def authorizer(self, context, *, action=InvestigationAction.CONTINUE):
        """绑定已确定的工具；每次实际授权仍重新核对案件权限和工具数据权限。"""
        def authorize(actor, tool_action, execution):
            try:
                check_runtime(context, action)
                trusted = self.execution_context(context.timeout_seconds)
                if actor != trusted.actor_id or execution != trusted:
                    return False
                return self.authorize(actor, tool_action, execution) is True
            except Exception:
                return False
        return authorize


def knowledge_tool(context: InvestigationRuntimeContext) -> KnowledgeSearchTool:
    """缺少绑定就拒绝；不使用默认工具、默认 profile 或模型提供的配置。"""
    for tool in context.tools:
        if tool.name == KnowledgeSearchTool.name and type(tool) is KnowledgeSearchTool:
            return tool
    raise InvestigationAccessError("当前运行未授予 knowledge_search 工具")


def knowledge_execution(context: InvestigationRuntimeContext, action: InvestigationAction,
                        planning_input=None) -> KnowledgeExecutionContext:
    check_runtime(context, action, planning_input)
    return knowledge_tool(context).execution_context(context.timeout_seconds)
