"""首轮知识执行与结构化 Observation；真实 Replanning 尚未实现。"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from openai import APITimeoutError, APIConnectionError, APIStatusError
from pydantic import ValidationError

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.contracts import (
    ErrorCategory, ExecutionError, ExecutionStatus, TaskExecutionResult,
)
from risk_platform.execution.tools import (
    ToolCall, ToolUnavailableError, tool_definition, available_tools, validate_tool_call, execute_registered_tool,
)
from risk_platform.workflow.runtime import InvestigationRuntimeContext, InvestigationAction, check_runtime
from risk_platform.workflow.contracts import InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.state import InvestigationState
from risk_platform.workflow.persistence.contracts import OperationType
from risk_platform.workflow.observation import (
    complete_observation, inconclusive_observation, summarize_executions,
    validate_round_inputs,
)


def initial_planning(
    state: InvestigationState,
    *,
    investigation_agent: InvestigationAgent,
    memory_context=None,
) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    options = {"memory_context": memory_context} if memory_context is not None and memory_context.memories else {}
    plan = investigation_agent.initial_plan(state["planning_input"], **options)

    # 新轮次清除旧的结束标记，案件保持开放。
    return {"investigation_plan": plan, "task_execution_results": [], "observation": None,
            "round_complete": False, "investigation_status": "OPEN"}


async def formulate_task_query(task, planning_input, *, goal, investigation_agent, runtime_context, memory_context=None):
    """复用 7B 查询形成与错误分类；8C 将其产物单独入账。"""
    try:
        offered = available_tools(task.capability, runtime_context)
        declarations = [tool.declaration() for tool in offered]
        limits = {tool.name: tool.model_input(runtime_context) for tool in offered}
        # 只传公开声明和参数限制，运行身份、权限回调不进入模型输入。
        async with asyncio.timeout(runtime_context.timeout_seconds):
            call = await asyncio.to_thread(
                investigation_agent.formulate_tool_call,
                task.model_copy(deep=True), planning_input.model_copy(deep=True),
                goal=goal, tools=declarations, tool_limits=limits,
                **({"memory_context": memory_context} if memory_context is not None and memory_context.memories else {}),
            )
        candidate = validate_tool_call(call, task.capability, runtime_context)
    except ToolUnavailableError as failure:
        error = ExecutionError(category=ErrorCategory.AUTHORIZATION if failure.code == "TOOL_NOT_BOUND" else ErrorCategory.VALIDATION,
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
        error = None
    return (candidate if error is None else None), error


@dataclass(frozen=True)
class QueryOutcome:
    """请求构造阶段产物：保留工具名称和专用参数，账本适配器负责版本校验。"""

    candidate: object = None
    tool_name: str | None = None
    failure: TaskExecutionResult | None = None


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
    execute_phase,
) -> dict[str, object]:
    """唯一任务循环；阶段执行器决定直接执行或通过账本复用、提交。

    持久化首次执行与恢复使用同一 ledger executor，无历史时创建新 Attempt。
    无默认直通执行器；生产由持久化图注入，单元测试显式提供阶段替身。
    """
    if not callable(execute_phase):
        raise TypeError("必须显式提供阶段执行器")
    phase = execute_phase
    plan = _business_model(InvestigationPlan, state["investigation_plan"])
    planning_input = _business_model(InitialPlanningInput, state["planning_input"])
    check_runtime(runtime_context, InvestigationAction.CONTINUE, planning_input)
    ids = [task.task_id for task in plan.tasks]
    if any(not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("正式任务 ID 必须非空且唯一")
    if not plan.tasks or any(task.status != TaskStatus.PENDING for task in plan.tasks):
        raise ValueError("首轮执行要求非空且全部待执行的正式任务")
    results, tasks = [], []
    for task in plan.tasks:
        check_runtime(runtime_context, InvestigationAction.CONTINUE, planning_input)

        async def query(*, memory_context=None):
            started = datetime.now(timezone.utc)
            candidate, error = await formulate_task_query(
                task, planning_input, goal=plan.goal, investigation_agent=investigation_agent, runtime_context=runtime_context,
                memory_context=memory_context,
            )
            if error is not None:
                return QueryOutcome(failure=TaskExecutionResult(
                    execution_id="exec-" + uuid4().hex, task_id=task.task_id, capability=task.capability,
                    status=ExecutionStatus.REJECTED if error.code in {"TOOL_NOT_BOUND", "UNSUPPORTED_CAPABILITY"} else ExecutionStatus.FAILED,
                    started_at=started, completed_at=datetime.now(timezone.utc), error=error,
                ))
            return QueryOutcome(candidate=candidate.arguments, tool_name=candidate.tool_name)

        formed = await phase(OperationType.QUERY_FORMULATION, query, task_id=task.task_id)
        if formed.failure is not None:
            result = formed.failure
        else:
            # 恢复后也保留所选工具名，不能把所有请求固定送往知识工具。
            selected = validate_tool_call(ToolCall(formed.tool_name, formed.candidate), task.capability, runtime_context)
            definition = tool_definition(selected.tool_name)
            async def execute_selected():
                result = await execute_registered_tool(
                    selected.tool_name, task, selected.arguments, runtime_context=runtime_context,
                )
                # 入账前校验身份，不能先保存错误结果再拒绝写 State。
                return _task_result(task, result)
            result = await phase(definition.execution_phase, execute_selected,
                                 task_id=task.task_id, candidate=selected.arguments, tool_name=selected.tool_name)
        result = _task_result(task, result)
        if any(previous.execution_id == result.execution_id for previous in results):
            raise ValueError("本轮执行记录 ID 不能重复")
        results.append(result)
        tasks.append(task.model_copy(update={"status": TaskStatus.COMPLETED
                     if result.status == ExecutionStatus.SUCCEEDED else TaskStatus.FAILED}))
    return {"investigation_plan": plan.model_copy(update={"tasks": tasks}),
            "task_execution_results": results}


async def observation(state: InvestigationState, *, investigation_agent: InvestigationAgent,
                      runtime_context: InvestigationRuntimeContext, execute_phase) -> dict[str, object]:
    """统一 Observation 阶段入口，分离解释逻辑与持久化机制。"""
    check_runtime(runtime_context, InvestigationAction.CONTINUE, _business_model(InitialPlanningInput, state["planning_input"]))
    plan = _business_model(InvestigationPlan, state["investigation_plan"])
    results = [_business_model(TaskExecutionResult, row) for row in state["task_execution_results"]]
    # 进入模型或持久化阶段前核验完整输入，不保存损坏的业务事实。
    plan, results = validate_round_inputs(plan, results)
    async def explain():
        update = await _interpret_observation(
            dict(investigation_plan=plan, task_execution_results=results),
            investigation_agent=investigation_agent, runtime_context=runtime_context,
        )
        return update["observation"]
    if not callable(execute_phase):
        raise TypeError("必须显式提供阶段执行器")
    output = await execute_phase(OperationType.OBSERVATION, explain, results=results)
    return {"observation": output}


async def _interpret_observation(
    state: InvestigationState, *, investigation_agent: InvestigationAgent,
    runtime_context: InvestigationRuntimeContext,
) -> dict[str, object]:
    """先校验执行事实，再解释并验证来源。没有知识或解释失败时明确不确定。"""
    plan, results = validate_round_inputs(state["investigation_plan"], state["task_execution_results"])
    summaries, _ = summarize_executions(results)
    if not any(item.outcome == "KNOWLEDGE_AVAILABLE" for item in summaries):
        return {"observation": inconclusive_observation(
            results, "NO_KNOWLEDGE_AVAILABLE", "本轮没有可用于解释的知识，未调用解释模型",
        )}
    try:
        async with asyncio.timeout(runtime_context.timeout_seconds):
            draft = await asyncio.to_thread(
                investigation_agent.observe_knowledge_round, plan.model_copy(deep=True),
                [result.model_copy(deep=True) for result in results],
            )
        output = complete_observation(results, draft)
    except (TimeoutError, APITimeoutError):
        output = inconclusive_observation(results, "OBSERVATION_TIMEOUT", "本轮知识解释超时")
    except (ValidationError, ValueError):
        output = inconclusive_observation(results, "OBSERVATION_INVALID", "本轮知识解释结构或来源引用不合法")
    except (APIConnectionError, APIStatusError):
        output = inconclusive_observation(results, "OBSERVATION_DEPENDENCY_FAILURE", "知识解释服务未能完成请求")
    except Exception:
        output = inconclusive_observation(results, "OBSERVATION_FAILED", "本轮知识解释未能完成")
    return {"observation": output}


def replanning(_: InvestigationState) -> dict[str, object]:
    """结束本轮 V0 骨架遍历；不表示案件调查完成，也不执行真实重规划。"""

    # Temporary V0 skeleton behavior: real replanning will decide whether to
    # continue the investigation after later workflow chapters are implemented.
    return {"round_complete": True, "investigation_status": "OPEN"}
