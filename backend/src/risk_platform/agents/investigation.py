"""单一 Investigation Agent：初始规划与知识查询构造，不直接执行工具。"""

import json
from pathlib import Path

from openai import OpenAI
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field
from risk_platform.core.observability import observe_generation, record_usage, record_langchain_usage

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
    SkillMaterialRef,
)


class _InitialTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    objective: str
    expected_result: str
    capability: CapabilityType = CapabilityType.KNOWLEDGE_RETRIEVAL
    skill_refs: tuple[SkillMaterialRef, ...] = ()


class _InitialPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str
    tasks: list[_InitialTaskDraft] = Field(min_length=1)


def initial_planning_request(planning_input, *, memory_context=None, previous_observations=(),
                             previous_tasks=(), remaining_task_budget=None, skill_catalogue=(), skill_messages=()):
    """规划预算预检与实际模型调用使用同一份输入组装。"""
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
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from risk_platform.agents.skills import read_skill, read_execution_result, PLANNING_SKILL_PROMPT
    context_payload["available_skills"] = [
        {"name": item["name"], "description": item["description"]} for item in skill_catalogue]
    messages = [
        {"role": "system", "content": INITIAL_PLANNING_SYSTEM_PROMPT + PLANNING_SKILL_PROMPT},
        {"role": "user", "content": json.dumps(context_payload, ensure_ascii=False)},
        *skill_messages,
    ]
    declarations = [convert_to_openai_tool(t) for t in (read_skill, read_execution_result)]
    return messages, declarations


class InvestigationAgent:
    def __init__(self, client: OpenAI, model: str, *, tool_model, skill_directory=None):
        from risk_platform.agents.skills import SkillDirectory
        self.client = client
        self.model = model
        self.tool_model = tool_model
        self.skill_directory = skill_directory if skill_directory is not None else SkillDirectory(Path(__file__).with_name("skill_library"))

    @observe_generation("knowledge-observation")
    def observe_round(self, plan, results, *, planning_input=None, previous_observations=(), available_capabilities=(), skill_materials=()):
        """只解释本轮真实结果；系统会再次校验所有引用，不赋予当前根因结论资格。"""
        from risk_platform.workflow.observation import ObservationDraft, build_observation_input, project_tool_results
        from risk_platform.core.token_budget import request_tokens, check_request_budget

        payload = build_observation_input(plan, results, planning_input=planning_input,
            previous_observations=previous_observations, available_capabilities=available_capabilities)
        payload["method_materials_not_case_evidence"] = list(skill_materials)
        messages = [{"role":"system", "content":OBSERVATION_SYSTEM_PROMPT},
                    {"role":"user", "content":json.dumps(payload, ensure_ascii=False)}]
        excess = request_tokens(messages) - 24000
        if excess > 0:
            # 保留任务、方法和历史约束，只缩减完整结果项；状态与省略信息不可删除。
            payload["executions"] = project_tool_results(results, max_tokens=max(1, 8192-excess-128))
            messages[1]["content"] = json.dumps(payload, ensure_ascii=False)
        check_request_budget(dict(messages=messages, tools=[]), input_limit=24000,
                             context_window=32768, output_tokens=4096, safety_tokens=1024)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"},
            max_tokens=4096,
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

    def formulate_tool_call(self, task, planning_input, *, goal, tools: list[StructuredTool], tool_limits, memory_context=None, skill_materials=(), definition_messages=None):
        """只看到本任务已绑定工具的公开声明与参数限制，不接受可信运行对象。"""
        from risk_platform.execution.tools import parse_tool_choice, required_tool_options
        names = tuple(tool.name for tool in tools)
        if not names or len(names) != len(set(names)):
            raise ValueError("必须明确提供当前任务可用的工具")
        # goal 和 objective：整体目标与当前任务
        payload = {
            "investigation_context": json.loads(build_initial_planning_context(planning_input)),
            "goal": goal,
            "objective": f"{task.objective}\n需要获取的信息：{task.expected_result}",
            "tool_limits": tool_limits,
            "method_materials_not_case_evidence": list(skill_materials),
        }
        if memory_context is not None:
            from risk_platform.memory.context import model_context
            payload["historical_memory"] = model_context(memory_context)
        if definition_messages is not None:
            from risk_platform.execution.metrics import DefinitionRequest
            from langchain_core.utils.function_calling import convert_to_openai_tool
            declaration = convert_to_openai_tool(DefinitionRequest)
            declaration["function"].update(name="list_metric_definitions",
                description="读取当前模型指标口径、分组和精确窗口；查询前必须先读。")
            tools = [*tools, declaration]
        tool_model = self.tool_model.bind_tools(tools, **required_tool_options())
        messages = [
            ("system", KNOWLEDGE_QUERY_SYSTEM_PROMPT),
            ("human", json.dumps(payload, ensure_ascii=False)),
        ]
        if definition_messages is not None:
            from risk_platform.execution.metrics import QUERY_INSTRUCTIONS
            messages.extend([{"role":"system", "content":QUERY_INSTRUCTIONS}, *definition_messages])
        from risk_platform.core.token_budget import check_request_budget
        from langchain_core.utils.function_calling import convert_to_openai_tool
        declarations = [convert_to_openai_tool(t) for t in tools]
        check_request_budget(dict(messages=messages, tools=declarations), input_limit=24000,
                             context_window=32768, output_tokens=4096, safety_tokens=1024)
        response = tool_model.invoke(messages, max_tokens=4096)
        record_langchain_usage(response)
        if (definition_messages is not None and len(response.tool_calls) == 1
                and response.tool_calls[0]["name"] == "list_metric_definitions"):
            if response.invalid_tool_calls or response.response_metadata.get("finish_reason") != "tool_calls":
                raise ValueError("指标目录请求不完整")
            return response.tool_calls[0]
        return parse_tool_choice(response, task.capability, allowed_names=names)

# | 参数 | 含义 | 例子 |
# |---|---|---|
# | `planning_input` | 原始调查问题或告警，以及可用能力 | 调查 credit-v3 的 PSI 异常 |
# | `memory_context` | 本次选中的历史记忆 | 以前调查过的相关情况 |
# | `previous_observations` | 此前各轮已提交的发现和未解决问题 | 尚不知道特征版本是否发生变化 |
# | `previous_tasks` | 以前实际执行过的任务和结果摘要 | 已检索标签规范，但零命中 |
# | `remaining_task_budget` | 还能安排多少个任务 | 最多再安排 4 个任务 |
    def initial_plan(
        self,
        planning_input: InitialPlanningInput,
        *, memory_context=None, previous_observations=(), previous_tasks=(), remaining_task_budget=None,
        skill_catalogue=(), skill_messages=(),
    ) -> InvestigationPlan | dict:
        """使用当前已注册且可用的能力生成候选调查任务。"""

        from risk_platform.core.token_budget import check_request_budget
        messages, declarations = initial_planning_request(planning_input, memory_context=memory_context,
            previous_observations=previous_observations, previous_tasks=previous_tasks,
            remaining_task_budget=remaining_task_budget, skill_catalogue=skill_catalogue, skill_messages=skill_messages)
        check_request_budget(dict(messages=messages, tools=declarations), input_limit=24000,
                             context_window=32768, output_tokens=4096, safety_tokens=1024)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=declarations,
            response_format={"type": "json_object"},
            max_tokens=4096,
        )
        record_usage(response)
        choice = response.choices[0]
        if choice.finish_reason == "tool_calls":
            message = choice.message.model_dump(include={"role", "content", "tool_calls"}, exclude_none=True)
            if not message.get("tool_calls"):
                raise ValueError("Planning tool response is empty")
            return message
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
                    skill_refs=task.skill_refs,
                )
                for task in draft.tasks
            ],
        )

    @observe_generation("planning-compaction")
    def compact_planning(self, messages, *, timeout_seconds):
        """只总结已经完成的旧读取交互；来源和正式材料仍由规划账本保存。"""
        from risk_platform.conversations.agent import SUMMARY_PROMPT
        from risk_platform.conversations.contracts import ConversationSummaryDraft
        from risk_platform.core.token_budget import check_request_budget, estimate_tokens, ContextBudgetExceeded
        request = [{"role":"system", "content":SUMMARY_PROMPT},
                   {"role":"user", "content":json.dumps(messages, ensure_ascii=False)}]
        check_request_budget(request, input_limit=24000, context_window=32768,
                             output_tokens=2048, safety_tokens=1024)
        response = self.client.chat.completions.create(model=self.model, messages=request,
            response_format={"type":"json_object"}, max_tokens=2048, timeout=timeout_seconds)
        record_usage(response)
        choice = response.choices[0]
        if choice.finish_reason != "stop" or choice.message.tool_calls:
            raise ValueError("规划摘要未完整返回")
        summary = ConversationSummaryDraft.model_validate_json(choice.message.content).summary
        if estimate_tokens(summary) > 2048:
            raise ContextBudgetExceeded("规划摘要超过预算")
        return summary

# 模型填写：
#     调查目标、任务目标、能力、预期结果

# 程序填写：
#     task_id、初始状态
