"""任务执行与结果解释；正式图根据已接受推进决定回到计划准备节点。"""

import asyncio
from functools import partial
from datetime import datetime, timezone
from uuid import uuid4

from openai import APITimeoutError, APIConnectionError, APIStatusError
from pydantic import ValidationError
from risk_platform.core.token_budget import ContextBudgetExceeded

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.contracts import (
    ErrorCategory, ExecutionError, ExecutionStatus, TaskExecutionResult, QueryOutcome,
)
from risk_platform.execution.tools import (
    ToolCall, ToolUnavailableError, tool_definition, available_tools, validate_tool_call,
)
from risk_platform.workflow.runtime import InvestigationRuntimeContext, InvestigationAction, check_runtime
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.state import InvestigationState
from risk_platform.workflow.observation import (
    complete_observation, inconclusive_observation, summarize_executions,
    validate_round_inputs,
)



# 筛选本次可用工具
#     ↓
# 提取工具说明和执行限制
#     ↓
# 调用模型选择工具、填写参数
#     ↓
# 根据当前上下文校验调用
#     ↓
# 返回：工具调用，或结构化错误
async def formulate_task_query(task, planning_input, *, goal, investigation_agent, runtime_context, memory_context=None, skill_materials=(), definition_reader=None, definition_events=()):
    """生成工具请求或失败事实；调用方无需再根据错误拼装阶段产物。"""
    started = datetime.now(timezone.utc)
    try:
        offered = available_tools(task.capability, runtime_context)
        declarations = [tool.tool for tool in offered]
        limits = {tool.tool.name: tool.execution_limits(runtime_context) for tool in offered}
        # 只传公开声明和参数限制，运行身份、权限回调不进入模型输入。
        async with asyncio.timeout(runtime_context.timeout_seconds):
            for _ in range(5):
                from risk_platform.execution.metric_reads import messages as read_messages
                call = await asyncio.to_thread(
                    investigation_agent.formulate_tool_call,
                    task.model_copy(deep=True), planning_input.model_copy(deep=True),
                    goal=goal, tools=declarations, tool_limits=limits,
                    **({"skill_materials": skill_materials} if skill_materials else {}),
                    **({"memory_context": memory_context} if memory_context is not None and memory_context.memories else {}),
                    **({"definition_messages": read_messages(definition_events)} if definition_reader else {}),
                )
                if not isinstance(call, dict):
                    break
                if not definition_reader or call.get("name") != "list_metric_definitions":
                    raise ValueError("请求阶段选择了未知读取工具")
                if any(event["call"]["id"] == call["id"] for event in definition_events):
                    raise ValueError("指标目录调用标识重复")
                event = await definition_reader(call)
                definition_events = (*definition_events, event)
            else:
                raise ValueError("指标定义读取循环达到上限")
            if call.tool_name == "query_model_metrics":
                from risk_platform.execution.metrics import MetricQuery, DefinitionPage
                request = MetricQuery.model_validate(call.arguments)
                definitions = [d for e in definition_events for d in DefinitionPage.model_validate(e["result"]).items]
                if not any(d.model_id == request.model_id and d.metric == request.metric
                    and d.version == request.definition_version and request.group_by in d.groups
                    and (request.start_date, request.end_date) in d.windows for d in definitions):
                    raise ValueError("查询必须对应本任务已读取的指标定义和精确窗口")
            # 中间等待过模型，所以返回后重新核对有意义。后续真正执行工具前仍要检查，尤其恢复时可能直接复用历史请求。
        candidate = validate_tool_call(call, task.capability, runtime_context)
    except ToolUnavailableError as failure:
        error = ExecutionError(category=ErrorCategory.AUTHORIZATION if failure.code in {"TOOL_NOT_BOUND", "TOOL_NOT_AUTHORIZED"} else ErrorCategory.VALIDATION,
                               code=failure.code, message="当前任务没有已绑定的可用工具", retryable=False)
    except (TimeoutError, APITimeoutError):
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_TIMEOUT",
                               message="工具请求构造超时，未执行工具", retryable=True)
    except (ValidationError, ValueError):
        error = ExecutionError(category=ErrorCategory.VALIDATION, code="QUERY_FORMULATION_INVALID",
                               message="工具请求构造结果非法，未执行工具", retryable=False)
    except APIConnectionError:
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_UNAVAILABLE",
                               message="工具请求构造服务不可用，未执行工具", retryable=True)
    except APIStatusError as failure:
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_API_FAILURE",
                               message="工具请求构造服务请求失败，未执行工具",
                               retryable=failure.status_code == 429 or failure.status_code >= 500)
    except Exception:
        error = ExecutionError(category=ErrorCategory.INTERNAL, code="QUERY_FORMULATION_FAILED",
                               message="工具请求构造未能完成，未执行工具", retryable=False)
    else:
        return QueryOutcome(candidate=candidate.arguments, tool_name=candidate.tool_name)
    return QueryOutcome(failure=TaskExecutionResult(
        execution_id="exec-" + uuid4().hex, task_id=task.task_id, capability=task.capability,
        status=ExecutionStatus.REJECTED if error.code in {"TOOL_NOT_BOUND", "TOOL_NOT_AUTHORIZED", "UNSUPPORTED_CAPABILITY"} else ExecutionStatus.FAILED,
        started_at=started, completed_at=datetime.now(timezone.utc), error=error,
    ))




def _business_model(model, value):
    # JSON 检查点与直接单元测试对象均重新经过契约校验。
    return model.model_validate_json(value.model_dump_json()) if hasattr(value, "model_dump_json") else model.model_validate(value)


def _task_result(task, result):
    result = TaskExecutionResult.model_validate_json(result.model_dump_json())
    if result.task_id != task.task_id or result.capability != task.capability:
        raise ValueError("执行结果与正式任务身份不一致")
    return result


async def task_execution(
    state: InvestigationState, *, investigation_agent: InvestigationAgent,
    runtime_context: InvestigationRuntimeContext,
    query_phase, tool_phase, skill_materials=(),
) -> dict[str, object]:
    """唯一任务循环；请求生成和工具执行分别使用显式注入的持久化入口。

    首次执行与恢复共用各阶段入口，无历史时创建新 Attempt。
    无默认直通执行器；生产由持久化图注入，单元测试显式提供阶段替身。
    """
    if not callable(query_phase) or not callable(tool_phase):
        raise TypeError("必须显式提供阶段执行器")
    plan = _business_model(InvestigationPlan, state["investigation_plan"])
    planning_input = _business_model(InitialPlanningInput, state["planning_input"])
    ids = [task.task_id for task in plan.tasks]
    if any(not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("正式任务 ID 必须非空且唯一")
    if not plan.tasks or any(task.status != TaskStatus.PENDING for task in plan.tasks):
        raise ValueError("首轮执行要求非空且全部待执行的正式任务")
    results, tasks = [], []
    for task in plan.tasks:
        # 每个任务开始前鉴权；前一任务可能经历过外部等待或恢复。
        check_runtime(runtime_context, InvestigationAction.CONTINUE, planning_input)

        # 延迟生成请求，阶段执行器可以先复用已提交产物。
        from risk_platform.workflow.planning import select_task_methods
        methods = select_task_methods((task,), skill_materials)
        query = partial(formulate_task_query, task, planning_input, goal=plan.goal,
                        investigation_agent=investigation_agent, runtime_context=runtime_context,
                        skill_materials=methods)
# 生产环境下：
# - 已有本轮选定的可靠结果：读取复用，不调用 query()。
# - 没有结果且策略允许：先登记尝试，再调用 query()，提交产物后返回。
# 所以，传入函数而不是提前算好结果，是为了让执行器有机会跳过重复模型调用。
        formed = await query_phase(query, task_id=task.task_id, capability=task.capability,
                                   **({"skill_materials": methods} if methods else {}))
        if formed.failure is not None:
            result = formed.failure
        else:
#  这里既可能拿到新生成的请求，也可能拿到恢复时读取的历史请求。
# 因此还要核对：这个工具及参数，在当前任务和运行上下文中是否仍然可用。
            selected = validate_tool_call(ToolCall(formed.tool_name, formed.candidate), task.capability, runtime_context)
            async def execute_selected(*, action_id=None, approved_material=None):
                result = await tool_definition(selected.tool_name).tool.ainvoke(
                    selected.arguments.model_dump(mode="json"),
                    config={"configurable": {"runtime": runtime_context, "task": task,
                        "action_id": action_id, "approved_material": approved_material}},
                )
                # 入账前校验身份，不能先保存错误结果再拒绝写 State。
                return _task_result(task, result)
            result = await tool_phase(execute_selected,
                                 task_id=task.task_id, candidate=selected.arguments, tool_name=selected.tool_name,
                                 capability=task.capability)
        result = _task_result(task, result)
        if any(previous.execution_id == result.execution_id for previous in results):
            raise ValueError("本轮执行记录 ID 不能重复")
        results.append(result)
        tasks.append(task.model_copy(update={"status": TaskStatus.COMPLETED
                     if result.status == ExecutionStatus.SUCCEEDED else TaskStatus.FAILED}))
    return {"investigation_plan": plan.model_copy(update={"tasks": tasks}),
            "task_execution_results": results}


async def observation(state: InvestigationState, *, investigation_agent: InvestigationAgent,
                      runtime_context: InvestigationRuntimeContext, observation_phase, previous_observations=(), skill_materials=()) -> dict[str, object]:
    """统一 Observation 阶段入口，分离解释逻辑与持久化机制。"""
    planning_input = _business_model(InitialPlanningInput, state["planning_input"])
    check_runtime(runtime_context, InvestigationAction.CONTINUE, planning_input)
    plan = _business_model(InvestigationPlan, state["investigation_plan"])
    results = [_business_model(TaskExecutionResult, row) for row in state["task_execution_results"]]
    # 进入模型或持久化阶段前核验完整输入，不保存损坏的业务事实。
    plan, results = validate_round_inputs(plan, results)
# | 材料 | 内容 |
# |---|---|
# | 原始调查背景 | credit-v3 的 PSI 出现异常，需要调查 |
# | 第一轮 Observation | 查到近期规则发生变更，但尚不清楚适用版本 |
# | 第二轮 Observation | 查到适用版本，但尚未确认异常时段是否已生效 |
    async def explain():
        return await _interpret_observation(
            plan, results, planning_input=planning_input,
            investigation_agent=investigation_agent, timeout_seconds=runtime_context.timeout_seconds,
            previous_observations=previous_observations,
            skill_materials=skill_materials,
            available_capabilities=tuple(capability for capability in planning_input.available_capabilities
                if any(tool_definition(binding.name).capability == capability for binding in runtime_context.tools)),
        )
    if not callable(observation_phase):
        raise TypeError("必须显式提供阶段执行器")
    output = await observation_phase(explain, results=results)
    # 这是供图更新 State 的字段，但图还会接着调用 advance_round()。
    return {"observation": output}

# graph.py：传入函数并预填依赖
#     ↓
# nodes.observation()：调用它
#     ↓
# phase_execution.observation_phase()：执行或复用解释
#     ↓
# _phase()：负责账本和事务

# | 函数 | 知道什么 |
# |---|---|
# | `explain()` | 怎样解释本轮结果 |
# | `observation_phase()` | 解释阶段需要登记哪些输入、保存什么产物 |
# | `_phase()` | 怎样查历史、登记尝试、提交结果和恢复 |

# observation() 已核验计划与结果；解释函数只消费已解析数据。
#     ↓
# 判断有没有可用于解释的材料
#     ├─ 没有 → 生成 INCONCLUSIVE
#     └─ 有 → 调用模型解释，再校验来源
#                   └─ 超时、非法输出等 → 生成 INCONCLUSIVE

async def _interpret_observation(
    plan, results, *, planning_input, investigation_agent: InvestigationAgent,
    timeout_seconds, previous_observations=(), available_capabilities=(), skill_materials=(),
):
    """解释已校验的本轮结果并验证引用；材料不足或模型失败时返回不确定。"""
    summaries, _ = summarize_executions(results)
    if not any(item.outcome in ("KNOWLEDGE_AVAILABLE", "RESULT_AVAILABLE") for item in summaries):
        return inconclusive_observation(
            results, "NO_KNOWLEDGE_AVAILABLE", "本轮没有可用于解释的知识，未调用解释模型",
        )
    try:
        async with asyncio.timeout(timeout_seconds):
#             同时提供：
# - 本轮计划和结果。
# - 原始调查背景。
# - 历史 Observation。
# - 当前绑定工具对应的可用能力。
            draft = await asyncio.to_thread(
                investigation_agent.observe_round, plan.model_copy(deep=True),
                [result.model_copy(deep=True) for result in results],
                planning_input=planning_input.model_copy(deep=True),
                previous_observations=previous_observations,
                available_capabilities=available_capabilities,
                **({"skill_materials": skill_materials} if skill_materials else {}),
            )
            # 完成正式 Observation 的构造和来源校验。
        output = complete_observation(results, draft)
    except ContextBudgetExceeded:
        output = inconclusive_observation(results, "OBSERVATION_INPUT_BUDGET_EXCEEDED", "工具结果超出解释输入预算，未形成结论")
    except (TimeoutError, APITimeoutError):
        output = inconclusive_observation(results, "OBSERVATION_TIMEOUT", "本轮知识解释超时")
    except (ValidationError, ValueError):
        output = inconclusive_observation(results, "OBSERVATION_INVALID", "本轮知识解释结构或来源引用不合法")
    except (APIConnectionError, APIStatusError):
        output = inconclusive_observation(results, "OBSERVATION_DEPENDENCY_FAILURE", "知识解释服务未能完成请求")
    except Exception:
        output = inconclusive_observation(results, "OBSERVATION_FAILED", "本轮知识解释未能完成")
    return output
