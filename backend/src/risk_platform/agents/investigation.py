"""单一 Investigation Agent：初始规划与知识查询构造，不直接执行工具。"""

import json

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field
from risk_platform.core.observability import observe_generation, record_usage

from risk_platform.agents.prompts import (
    INITIAL_PLANNING_SYSTEM_PROMPT,
    KNOWLEDGE_QUERY_SYSTEM_PROMPT,
    OBSERVATION_SYSTEM_PROMPT,
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
    capability: CapabilityType = CapabilityType.KNOWLEDGE_RETRIEVAL


class _InitialPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    tasks: list[_InitialTaskDraft] = Field(min_length=1)


class InvestigationAgent:
    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    @observe_generation("knowledge-observation")
    def observe_round(self, plan, results, *, planning_input=None, previous_observations=(), available_capabilities=()):
        """只解释本轮真实结果；系统会再次校验所有引用，不赋予当前根因结论资格。"""
        from risk_platform.workflow.observation import ObservationDraft, build_observation_input
        from risk_platform.core.token_budget import ContextBudgetExceeded

        payload = build_observation_input(plan, results, planning_input=planning_input,
            previous_observations=previous_observations, available_capabilities=available_capabilities)
        if any(item["outcome"] in ("KNOWLEDGE_AVAILABLE", "RESULT_AVAILABLE") for item in payload["executions"]) and not any(
            item.get("items") for item in payload["executions"]
        ):
            raise ContextBudgetExceeded("工具正文全部超出预算，未调用解释模型")

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": OBSERVATION_SYSTEM_PROMPT},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            response_format={"type": "json_object"},
        )
        record_usage(response)
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ValueError("本轮解释未完整返回")
        draft = ObservationDraft.model_validate_json(choice.message.content)
        shown = {item["execution_id"]: {c["reference_id"] for c in item.get("items", [])}
                 for item in payload["executions"]}
        for finding in draft.findings:
            for source in finding.sources:
                reference = source.reference if hasattr(source, "reference") else {"reference_id": source.context_id}
                reference_id = reference.get("reference_id") or reference.get("context_id")
                if reference_id not in shown.get(source.execution_id, set()):
                    raise ValueError("模型引用了未提供的工具结果项")
        return draft

    def formulate_tool_call(self, task, planning_input, *, goal, tools, tool_limits, memory_context=None):
        """只看到本任务已绑定工具的公开声明与参数限制，不接受可信运行对象。"""
        from risk_platform.execution.tools import parse_tool_choice, required_tool_options
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
            tools=tools, **required_tool_options(),
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
        *, memory_context=None, previous_observations=(), previous_tasks=(), remaining_task_budget=None,
    ) -> InvestigationPlan:
        """使用当前已注册且可用的能力生成候选调查任务。"""

        from risk_platform.execution.tools import TOOL_DEFINITIONS
        supported = {d.capability for d in TOOL_DEFINITIONS.values()}
        if not set(planning_input.available_capabilities) & supported:
            raise ValueError(
                "初始计划没有可用的已注册工具能力"
            )

        context_payload = json.loads(build_initial_planning_context(planning_input))
        context_payload["available_capabilities"] = [c.value for c in planning_input.available_capabilities if c in supported]
        # 保留各份 Observation 来源；单个 Agent 的 STOP 不是全局停止。
        if previous_observations:
            from risk_platform.workflow.observation import RoundObservation
            observations = [RoundObservation.model_validate_json(o.model_dump_json()) for o in previous_observations]
            if len({o.observation_id for o in observations}) != len(observations):
                raise ValueError("规划来源 Observation 不能重复")
            if remaining_task_budget is None or type(remaining_task_budget) is not int or remaining_task_budget <= 0:
                raise ValueError("后续规划必须提供正整数任务预算")
            context_payload["observations"] = [o.model_dump(mode="json") for o in observations]
            context_payload["previous_tasks"] = list(previous_tasks)
            context_payload["remaining_task_budget"] = remaining_task_budget

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
        if remaining_task_budget is not None and len(draft.tasks) > remaining_task_budget:
            raise ValueError("候选任务超过剩余预算")
        if any(task.capability not in planning_input.available_capabilities for task in draft.tasks):
            raise ValueError("候选计划使用了未提供的能力")
        return InvestigationPlan(
            goal=draft.goal,
            tasks=[
                InvestigationTask(
                    objective=task.objective,
                    capability=task.capability,
                    expected_result=task.expected_result,
                )
                for task in draft.tasks
            ],
        )
