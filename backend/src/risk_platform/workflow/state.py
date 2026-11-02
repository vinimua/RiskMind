"""Minimal state contract for the V0 investigation workflow."""

from typing import Literal, NotRequired, TypedDict

from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan


class InvestigationState(TypedDict):
    """State shared by the nodes in the investigation skeleton."""

    planning_input: InitialPlanningInput
    investigation_plan: NotRequired[InvestigationPlan]
    # 只表示当前骨架轮次结束，不证明知识任务已执行或案件已完成。
    round_complete: NotRequired[bool]
    # 现阶段没有案件关闭能力；后续真实生命周期实现时再扩展状态。
    investigation_status: NotRequired[Literal["OPEN"]]
