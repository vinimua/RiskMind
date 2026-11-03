"""每次调用的可信控制信息；不写入 State 或 checkpoint。"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from risk_platform.workflow.runtime import InvestigationRuntimeContext

if TYPE_CHECKING:
    from risk_platform.workflow.recovery import _Invocation


@dataclass(frozen=True)
class InvestigationInvocation:
    runtime: InvestigationRuntimeContext
    recovery: "_Invocation | None" = None
