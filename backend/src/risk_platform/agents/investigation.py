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

    def formulate_tool_call(self, task, planning_input, *, goal, tools, tool_limits, memory_context=None):
        """只看到本任务已绑定工具的公开声明与参数限制，不接受可信运行对象。"""
        from risk_platform.execution.tools import parse_tool_choice
        names = tuple(tool["function"]["name"] for tool in tools)
        if not names or len(names) != len(set(names)):
            raise ValueError("必须明确提供当前任务可用的工具")
        payload = {
            "investigation_context": json.loads(build_initial_planning_context(planning_input)),
            "goal": goal, "objective": task.objective,
            "expected_result": task.expected_result, "tool_limits": tool_limits,
        }
        if memory_context is not None:
            from risk_platform.memory.context import model_context
            payload["historical_memory"] = model_context(memory_context)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": KNOWLEDGE_QUERY_SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            tools=tools, tool_choice="required", parallel_tool_calls=False,
        )
        return parse_tool_choice(response.choices[0], task.capability, allowed_names=names)

    def formulate_knowledge_query(
        self, task: InvestigationTask, planning_input: InitialPlanningInput,
        *, goal: str, max_top_k: int,
    ):
        """独立知识查询构造接口；workflow 使用通用 formulate_tool_call。"""
        from risk_platform.execution.tools import tool_declarations
        if task.capability != CapabilityType.KNOWLEDGE_RETRIEVAL:
            raise ValueError("Query Formulation 只支持知识检索任务")
        call = self.formulate_tool_call(task, planning_input, goal=goal,
            tools=tool_declarations(task.capability, names=("knowledge_search",)),
            tool_limits={"knowledge_search": {"max_top_k": max_top_k}})
        if call.arguments.top_k > max_top_k:
            raise ValueError("工具请求数量超过本次允许上限")
        return call.arguments

    def initial_plan(
        self,
        planning_input: InitialPlanningInput,
        *, memory_context=None,
    ) -> InvestigationPlan:
        """Plan the knowledge needed for the first investigation round."""

        if CapabilityType.KNOWLEDGE_RETRIEVAL not in planning_input.available_capabilities:
            raise ValueError(
                "初始计划生成需要 KNOWLEDGE_RETRIEVAL 能力,但可用能力里没有"
            )

        context_payload = json.loads(build_initial_planning_context(planning_input))
        if memory_context is not None:
            from risk_platform.memory.context import model_context
            context_payload["historical_memory"] = model_context(memory_context)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": INITIAL_PLANNING_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": json.dumps(context_payload, ensure_ascii=False),
                },
            ],
            response_format={"type": "json_object"},
        )

        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise ValueError("初始计划未完整返回，不能接受截断或中止的草案")
        content = choice.message.content
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
