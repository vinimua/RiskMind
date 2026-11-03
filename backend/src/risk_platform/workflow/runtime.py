"""通用可信运行环境；工具配置由各工具独立定义，不来自模型或 checkpoint。"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class TrustedToolBinding(Protocol):
    """可信应用安装的工具绑定，只校验归属，不由模型声明或动态导入。"""

    name: str

    def validate_binding(self, actor_id: str, resource: InvestigationResource) -> None: ...


@dataclass(frozen=True)
class InvestigationRuntimeContext:
    """案件级身份和授权；tools 只包含本次由应用明确授予的工具绑定。"""

    investigation: InvestigationRecord
    round: RoundRecord
    actor_id: str
    authorize_resource: Callable[[str, str, InvestigationResource], bool]
    tools: tuple[TrustedToolBinding, ...] = ()
    timeout_seconds: float = 30.0


def check_runtime(context: InvestigationRuntimeContext, action: InvestigationAction,
                  planning_input: InitialPlanningInput | None = None) -> InvestigationRuntimeContext:
    """核对通用身份、案件、轮次和资源权限，再检查工具绑定；不授予工具执行权限。"""
    if not isinstance(context, InvestigationRuntimeContext):
        raise InvestigationAccessError("必须按次提供可信运行上下文")
    if not isinstance(context.actor_id, str) or not context.actor_id.strip():
        raise InvestigationAccessError("执行身份不能为空")
    timeout = context.timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise InvestigationAccessError("运行超时必须为有限正数")
    investigation = InvestigationRecord.model_validate_json(context.investigation.model_dump_json())
    round_record = RoundRecord.model_validate_json(context.round.model_dump_json())
    if round_record.investigation_id != investigation.investigation_id:
        raise InvestigationAccessError("调查与轮次归属不一致")
    source = investigation.planning_input
    model_id = source.monitoring_alert.model_id if source.monitoring_alert else source.user_query.model_id
    if planning_input is not None and InitialPlanningInput.model_validate_json(planning_input.model_dump_json()) != source:
        raise InvestigationAccessError("本次规划输入与可信调查记录不一致")
    resource = InvestigationResource(investigation.investigation_id, round_record.round_id, model_id)
    try:
        allowed = context.authorize_resource(context.actor_id, InvestigationAction(action).value, resource)
    except Exception:
        raise InvestigationAccessError("调查权限检查未能完成") from None
    if allowed is not True:
        raise InvestigationAccessError("当前身份没有所请求的调查权限")
    if not isinstance(context.tools, tuple):
        raise InvestigationAccessError("工具绑定必须为固定元组")
    names = set()
    for tool in context.tools:
        if not isinstance(tool, TrustedToolBinding) or not isinstance(tool.name, str) or not tool.name.strip() or tool.name in names:
            raise InvestigationAccessError("工具绑定类型非法或名称重复")
        names.add(tool.name)
        try:
            tool.validate_binding(context.actor_id, resource)
        except Exception:
            raise InvestigationAccessError("工具与可信身份或案件范围不一致") from None
    return context
