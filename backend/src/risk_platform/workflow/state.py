"""首轮调查共享状态：规划输入、正式任务、执行事实及轮次结束标记。"""

from typing import Literal, NotRequired, TypedDict

from risk_platform.execution.contracts import TaskExecutionResult

from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan


class InvestigationState(TypedDict):
    """State shared by the nodes in the investigation skeleton."""

    planning_input: InitialPlanningInput
    investigation_plan: NotRequired[InvestigationPlan]
    # 仅保存本轮执行事实，初始规划时重置；可信身份/鉴权器不放入模型可写 State。
    task_execution_results: NotRequired[list[TaskExecutionResult]]
    # 表示本轮图调用结束，不保证每个任务成功，更不代表案件已完成。
    round_complete: NotRequired[bool]
    # 现阶段没有案件关闭能力；后续真实生命周期实现时再扩展状态。
    investigation_status: NotRequired[Literal["OPEN"]]
