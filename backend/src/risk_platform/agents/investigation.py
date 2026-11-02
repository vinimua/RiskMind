"""单一 Investigation Agent：初始规划与知识查询构造，不直接执行工具。"""

import json

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from risk_platform.agents.prompts import (
    INITIAL_PLANNING_SYSTEM_PROMPT,
    KNOWLEDGE_QUERY_SYSTEM_PROMPT,
    KNOWLEDGE_OBSERVATION_SYSTEM_PROMPT,
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

    def observe_knowledge_round(self, plan, results):
        """只解释本轮真实结果；系统会再次校验所有引用，不赋予当前根因结论资格。"""
        from risk_platform.workflow.observation import KnowledgeObservationDraft, build_observation_input

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": KNOWLEDGE_OBSERVATION_SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(build_observation_input(plan, results), ensure_ascii=False)}],
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError("本轮解释未完整返回")
        return KnowledgeObservationDraft.model_validate_json(choice.message.content)

    def formulate_knowledge_query(
        self, task: InvestigationTask, planning_input: InitialPlanningInput,
        *, goal: str, max_top_k: int,
    ):
        """将任务意图转换为候选请求；可信身份、策略与物理集合不交给模型决定。"""
        from risk_platform.execution.contracts import KnowledgeSearchToolRequest

        if task.capability != CapabilityType.KNOWLEDGE_RETRIEVAL:
            raise ValueError("Query Formulation 只支持知识检索任务")
        payload = {
            "investigation_context": json.loads(build_initial_planning_context(planning_input)),
            "goal": goal, "objective": task.objective,
            "expected_result": task.expected_result, "max_top_k": max_top_k,
        }
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": KNOWLEDGE_QUERY_SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            response_format={"type": "json_object"},
        )
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError("查询构造未完整返回")
        return KnowledgeSearchToolRequest.model_validate_json(choice.message.content)

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
