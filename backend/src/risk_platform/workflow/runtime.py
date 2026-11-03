"""每次图调用的可信上下文。由已认证应用构造，不从 State/模型输出恢复权限。"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.workflow.contracts import InitialPlanningInput
from risk_platform.workflow.persistence.contracts import InvestigationRecord, RoundRecord


class InvestigationAction(StrEnum):
    READ = "investigation:read"
    CONTINUE = "investigation:continue"


class InvestigationAccessError(PermissionError):
    """权限/资源绑定失败，不暴露策略异常正文。"""


@dataclass(frozen=True)
class InvestigationResource:
    investigation_id: str
    round_id: str
    model_id: str | None


@dataclass(frozen=True)
class InvestigationRuntimeContext:
    # 必须来自可信应用的 canonical 调查记录；load_runtime_context 提供 PG 加载入口。
    investigation: InvestigationRecord
    round: RoundRecord
    execution: KnowledgeExecutionContext
    authorize_resource: Callable[[str, str, InvestigationResource], bool]
    authorize_knowledge: Callable[[str, str, KnowledgeExecutionContext], bool]
    allow_unscoped: bool = False


def check_runtime(context: InvestigationRuntimeContext, action: InvestigationAction,
                  planning_input: InitialPlanningInput | None = None) -> KnowledgeExecutionContext:
    """在任何模型/工具调用前核对身份、目标及当前授权；True 以外都拒绝。"""
    if not isinstance(context, InvestigationRuntimeContext):
        raise InvestigationAccessError("必须按次提供可信运行上下文")
    investigation = InvestigationRecord.model_validate_json(context.investigation.model_dump_json())
    round_record = RoundRecord.model_validate_json(context.round.model_dump_json())
    trusted = KnowledgeExecutionContext.model_validate_json(context.execution.model_dump_json())
    if round_record.investigation_id != investigation.investigation_id:
        raise InvestigationAccessError("调查与轮次归属不一致")
    source = investigation.planning_input
    model_id = source.monitoring_alert.model_id if source.monitoring_alert else source.user_query.model_id
    if model_id is not None:
        if trusted.scope.model_id != model_id:
            raise InvestigationAccessError("调查目标与可信 model_id 范围必须一致")
    elif context.allow_unscoped is not True:
        raise InvestigationAccessError("无模型目标的调查必须显式授权运行范围")
    if planning_input is not None and InitialPlanningInput.model_validate_json(planning_input.model_dump_json()) != source:
        raise InvestigationAccessError("本次规划输入与可信调查记录不一致")
    resource = InvestigationResource(investigation.investigation_id, round_record.round_id, model_id)
    try:
        allowed = context.authorize_resource(trusted.actor_id, InvestigationAction(action).value, resource)
    except Exception:
        raise InvestigationAccessError("调查权限检查未能完成") from None
    if allowed is not True:
        raise InvestigationAccessError("当前身份没有所请求的调查权限")
    return trusted


def knowledge_authorizer(context: InvestigationRuntimeContext):
    """7A 的知识权限保留；调查继续权限不能被知识权限替代。"""
    def authorize(actor, action, execution):
        try:
            trusted = check_runtime(context, InvestigationAction.CONTINUE)
        except InvestigationAccessError:
            return False
        if actor != trusted.actor_id or execution != trusted:
            return False
        return context.authorize_knowledge(actor, action, execution) is True
    return authorize
