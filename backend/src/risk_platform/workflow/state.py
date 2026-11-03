"""首轮调查共享状态：规划输入、正式任务、执行事实及轮次结束标记。"""

from typing import Literal, NotRequired, TypedDict

from risk_platform.execution.contracts import TaskExecutionResult

from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan
from risk_platform.workflow.observation import RoundObservation


class InvestigationState(TypedDict):
    """唯一调查图的共享业务状态。

    正式图使用 JSON 字典/列表保存检查点，业务函数重新校验为类型化对象。
    保留对象类型以便直接测试业务函数；不存在由 State 选择的非持久化执行模式。
    """

    planning_input: InitialPlanningInput | dict
    investigation_plan: NotRequired[InvestigationPlan | dict]
    # 仅保存本轮执行事实，初始规划时重置；可信身份/鉴权器不放入模型可写 State。
    task_execution_results: NotRequired[list[TaskExecutionResult] | list[dict]]
    # 7C 解释结果；None 表示本轮尚未解释，避免复用上一轮的 Observation。
    observation: NotRequired[RoundObservation | dict | None]
    # 表示本轮图调用结束，不保证每个任务成功，更不代表案件已完成。
    round_complete: NotRequired[bool]
    # 现阶段没有案件关闭能力；后续真实生命周期实现时再扩展状态。
    investigation_status: NotRequired[Literal["OPEN"]]

    # 恢复入口的 JSON 检查点身份；仅用于核验，不能决定执行模式或授予权限。
    checkpoint_schema_version: NotRequired[int]
    run_id: NotRequired[str]
    plan_id: NotRequired[str]
