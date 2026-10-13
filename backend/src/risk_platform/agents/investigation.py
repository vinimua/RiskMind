"""Investigation Agent for the first investigation-planning round."""

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from risk_platform.agents.prompts import (
    INITIAL_PLANNING_SYSTEM_PROMPT,
    build_initial_planning_context,
)
from risk_platform.workflow.contracts import (
    CapabilityType,
    InitialPlanningInput,
    InvestigationPlan,
    InvestigationTask,
)


class _InitialTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    expected_result: str


class _InitialPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    tasks: list[_InitialTaskDraft] = Field(min_length=1)


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

        draft = _InitialPlanDraft.model_validate_json(content)
        return InvestigationPlan(
            goal=draft.goal,
            tasks=[
                InvestigationTask(
                    objective=task.objective,
                    capability=CapabilityType.KNOWLEDGE_RETRIEVAL,
                    expected_result=task.expected_result,
                )
                for task in draft.tasks
            ],
        )
