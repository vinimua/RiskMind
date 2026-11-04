"""每次调用的可信控制信息；不写入 State 或 checkpoint。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationRuntimeContext

if TYPE_CHECKING:
    from risk_platform.workflow.recovery import _Invocation

# runtime
# # 谁在运行？哪个案件？允许使用哪些工具？

# execution：内部持久化执行控制；首次执行和恢复均使用。
# # 正在执行哪次运行？是否持有执行权？
# # 已有结果能否复用？失败后是否允许重试？
@dataclass(frozen=True)
class InvestigationInvocation:
    """唯一调用上下文；execution 只能由受控持久化入口在 claim 后填入。"""

    runtime: InvestigationRuntimeContext
    execution: "_Invocation | None" = None


def require_invocation(value, *, claimed=False) -> InvestigationInvocation:
    """拒绝旧裸 Runtime 和伪造包装；不隐式选择另一种执行模式。"""
    if not isinstance(value, InvestigationInvocation) or not isinstance(value.runtime, InvestigationRuntimeContext):
        raise InvestigationAccessError("必须显式提供 InvestigationInvocation 和可信 runtime")
    if claimed and value.execution is None:
        raise InvestigationAccessError("正式图只能通过持久化 runner 在取得运行权后调用")
    if value.execution is not None:
        if not claimed:
            raise InvestigationAccessError("调用方不能预先提供内部执行 claim")
        from risk_platform.workflow.recovery import _Invocation
        if not isinstance(value.execution, _Invocation) or value.execution.context is not value.runtime:
            raise InvestigationAccessError("执行 claim 与可信身份不一致")
    return value
