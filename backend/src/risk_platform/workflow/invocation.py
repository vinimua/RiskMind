"""每次调用的可信控制信息；不写入 State 或 checkpoint。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationRuntimeContext

if TYPE_CHECKING:
    from risk_platform.workflow.recovery import _Invocation


@dataclass(frozen=True)
class InvestigationInvocation:
    """唯一调用上下文；recovery 只能由受控持久化入口在 claim 后填入。"""

    runtime: InvestigationRuntimeContext
    recovery: "_Invocation | None" = None


def require_invocation(value, *, claimed=False) -> InvestigationInvocation:
    """拒绝旧裸 Runtime 和伪造包装；不隐式选择另一种执行模式。"""
    if not isinstance(value, InvestigationInvocation) or not isinstance(value.runtime, InvestigationRuntimeContext):
        raise InvestigationAccessError("必须显式提供 InvestigationInvocation 和可信 runtime")
    if value.recovery is not None:
        if not claimed:
            raise InvestigationAccessError("调用方不能预先提供内部恢复 claim")
        from risk_platform.workflow.recovery import _Invocation
        if not isinstance(value.recovery, _Invocation) or value.recovery.context is not value.runtime:
            raise InvestigationAccessError("恢复 claim 与可信身份不一致")
    return value
