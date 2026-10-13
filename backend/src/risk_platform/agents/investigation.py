"""Investigation Agent for the first investigation-planning round."""

from openai import OpenAI

from risk_platform.agents.prompts import (
    INITIAL_PLANNING_SYSTEM_PROMPT,
    build_initial_planning_context,
)
from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
)


class InvestigationAgent:
    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def initial_plan(
        self,
        planning_input: InitialPlanningInput,
    ) -> InvestigationPlan:
        """Plan the knowledge needed for the first investigation round."""

        if CapabilityType.KNOWLEDGE_RETRIEVAL not in planning_input.available_capabilities:
            raise ValueError(
                "初始计划生成需要 KNOWLEDGE_RETRIEVAL 能力,但可用能力里没有"
            )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": INITIAL_PLANNING_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": build_initial_planning_context(planning_input),
                },
            ],
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        if not content:
            raise ValueError("初始计划生成失败,模型没有返回任何内容")

        plan = InvestigationPlan.model_validate_json(content)
        if not plan.tasks:
            raise ValueError("初始计划生成必须返回至少一个任务,但模型返回了空任务列表")

        invalid_capabilities = {
            task.capability
            for task in plan.tasks
            if task.capability is not CapabilityType.KNOWLEDGE_RETRIEVAL
        }
        if invalid_capabilities:
            values = ", ".join(sorted(capability.value for capability in invalid_capabilities))
            raise ValueError(
                "初始计划生成返回了禁止的任务能力: " + values
            )

        return plan
