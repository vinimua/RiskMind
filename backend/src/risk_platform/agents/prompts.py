"""Prompt definitions and context builders for investigation agents."""

from risk_platform.workflow.contracts import InitialPlanningInput


INITIAL_PLANNING_SYSTEM_PROMPT = """
你是 RiskMind 的 Investigation Agent，当前执行 Initial Planning。

你的任务不是判断异常根因，而是根据当前异常规划第一轮知识检索任务：决定为了进一步调查，
应该先从知识库获取哪些已有知识。可以关注历史相似异常案例、当前模型背景、模型已知限制、
历史问题、历史调查过程、指标相关异常经验，以及与当前异常相关的其他既有知识；无需每次覆盖
全部方向。

当前阶段必须遵守以下规则：
- 不判断最终根因，不生成 Hypothesis；
- 不声称已经查询过知识库，不编造 KnowledgeHit 或数据库事实；
- 不执行 Tool、SQL，也不计算 KS、PSI 或其他指标；
- 只能创建 capability 为 KNOWLEDGE_RETRIEVAL 的任务；
- 不得创建 FACT_RETRIEVAL 或 COMPUTATION 任务；
- 至少创建一个任务。

只返回一个 JSON 对象，不要输出 Markdown 或额外文字。JSON 必须符合以下结构：
{
  "goal": "字符串",
  "tasks": [
    {
      "objective": "字符串",
      "capability": "KNOWLEDGE_RETRIEVAL",
      "expected_result": "字符串"
    }
  ]
}
不要输出 task_id 或 status，它们由系统生成。
""".strip()


def _provided(value: object | None) -> str:
    return "not provided" if value is None else str(value)


def build_initial_planning_context(planning_input: InitialPlanningInput) -> str:
    """Build the user message for an Initial Planning request."""

    if planning_input.user_query is not None:
        query = planning_input.user_query
        return "\n".join(
            [
                "source = USER_QUERY",
                f"query = {query.query}",
                f"model_id = {_provided(query.model_id)}",
                f"time_window = {_provided(query.time_window)}",
            ]
        )

    alert = planning_input.monitoring_alert
    if alert is None:
        raise ValueError("初始计划生成需要一个用户查询或监控告警,但两者都没提供")

    return "\n".join(
        [
            "source = MONITORING_ALERT",
            f"alert_id = {alert.alert_id}",
            f"model_id = {alert.model_id}",
            f"metric = {alert.metric}",
            f"symptom = {alert.symptom}",
            f"time_window = {alert.time_window}",
            f"current_value = {alert.current_value}",
            f"reference_value = {alert.reference_value}",
            f"triggered_at = {alert.triggered_at.isoformat()}",
        ]
    )
