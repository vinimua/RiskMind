"""每次调用的可信控制信息；不写入 State 或 checkpoint。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationRuntimeContext

if TYPE_CHECKING:
    from risk_platform.workflow.run_guard import RunExecutionContext

# runtime
# # 谁在运行？哪个案件？允许使用哪些工具？

# execution：内部持久化执行控制；首次执行和恢复均使用。
# # 正在执行哪次运行？是否持有执行权？
# # 已有结果能否复用？失败后是否允许重试？
@dataclass(frozen=True)
class InvestigationInvocation:
    """唯一调用上下文；execution 只能由受控持久化入口在 claim 后填入。"""

    runtime: InvestigationRuntimeContext
    execution: "RunExecutionContext | None" = None
# 上层应用创建 trusted_runtime
#     ↓
# InvestigationInvocation(runtime=trusted_runtime)
#     ↓
# Runner 检查、鉴权、取得运行锁
#     ↓
# 新包装：原 runtime + 内部 execution
#     ↓
# LangGraph 把它放在 runtime.context 中
#     ↓
# 节点读取 call.runtime

def require_invocation(value, *, claimed=False) -> InvestigationInvocation:
    """拒绝旧裸 Runtime 和伪造包装；不隐式选择另一种执行模式。"""
    if not isinstance(value, InvestigationInvocation) or not isinstance(value.runtime, InvestigationRuntimeContext):
        raise InvestigationAccessError("必须显式提供 InvestigationInvocation 和可信 runtime")
    if claimed and value.execution is None:
        raise InvestigationAccessError("正式图只能通过持久化 runner 在取得运行权后调用")
    if value.execution is not None:
        if not claimed:
            raise InvestigationAccessError("调用方不能预先提供内部执行 claim")
        from risk_platform.workflow.run_guard import RunExecutionContext
        if not isinstance(value.execution, RunExecutionContext):
            raise InvestigationAccessError("内部执行控制类型不合法")
        identity = value.execution.identity
        if (identity.investigation_id != value.runtime.investigation.investigation_id
                or identity.round_id != value.runtime.round.round_id
                or value.execution.guard.run_id != identity.run_id):
            raise InvestigationAccessError("执行 claim 与可信身份不一致")
    return value
# 应用提供可信 runtime
#     ↓
# runner 鉴权、取得运行锁
#     ↓
# 创建内部 execution
#     ↓
# 包装成 InvestigationInvocation
#     ↓
# 调用图
#     ↓
# 这里检查包装是否正确