"""首轮知识执行与结构化 Observation；真实 Replanning 尚未实现。"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from openai import APITimeoutError, APIConnectionError, APIStatusError
from pydantic import ValidationError

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.contracts import (
    ErrorCategory, ExecutionError, ExecutionStatus, KnowledgeExecutionContext, TaskExecutionResult,
)
from risk_platform.execution.tools import KNOWLEDGE_SEARCH, execute_registered_tool
from risk_platform.workflow.contracts import CapabilityType, InitialPlanningInput, InvestigationPlan, TaskStatus
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
) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    plan = investigation_agent.initial_plan(state["planning_input"])

    # 新轮次清除旧的结束标记，案件保持开放。
    return {"investigation_plan": plan, "task_execution_results": [], "observation": None,
            "round_complete": False, "investigation_status": "OPEN"}


async def formulate_task_query(task, planning_input, *, goal, investigation_agent, trusted):
    """复用 7B 查询形成与错误分类；8C 将其产物单独入账。"""
    try:
        if task.capability == CapabilityType.KNOWLEDGE_RETRIEVAL:
            # 现有 Agent 使用同步 SDK，移到线程避免阻塞事件循环；不把 objective 当作 query。
            async with asyncio.timeout(trusted.timeout_seconds):
                candidate = await asyncio.to_thread(
                    investigation_agent.formulate_knowledge_query,
                    task.model_copy(deep=True), planning_input.model_copy(deep=True),
                    goal=goal, max_top_k=trusted.max_top_k,
                )
        else:
            # 不运行其他能力，也不擅自把它改成知识任务；交给 7A 明确拒绝。
            candidate = {}
    except (TimeoutError, APITimeoutError):
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_TIMEOUT",
                               message="知识查询构造超时，未调用检索工具", retryable=True)
    except (ValidationError, ValueError):
        error = ExecutionError(category=ErrorCategory.VALIDATION, code="QUERY_FORMULATION_INVALID",
                               message="知识查询构造结果非法，未调用检索工具", retryable=False)
    except APIConnectionError:
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_UNAVAILABLE",
                               message="知识查询构造服务不可用，未调用检索工具", retryable=True)
    except APIStatusError as failure:
        error = ExecutionError(category=ErrorCategory.DEPENDENCY, code="QUERY_FORMULATION_API_FAILURE",
                               message="知识查询构造服务请求失败，未调用检索工具",
                               retryable=failure.status_code == 429 or failure.status_code >= 500)
    except Exception:
        error = ExecutionError(category=ErrorCategory.INTERNAL, code="QUERY_FORMULATION_FAILED",
                               message="知识查询构造未能完成，未调用检索工具", retryable=False)
    else:
        error = None
    return (candidate if error is None else None), error


@dataclass(frozen=True)
class QueryOutcome:
    """内部查询阶段结果；原始候选仅交给 7A 或账本适配器校验。"""

    candidate: object = None
    failure: TaskExecutionResult | None = None


def _business_model(model, value):
    # 两种入口共用节点，JSON 字典与业务对象均重新经过契约校验。
    return model.model_validate_json(value.model_dump_json()) if hasattr(value, "model_dump_json") else model.model_validate(value)


def _task_result(task, result):
    result = TaskExecutionResult.model_validate_json(result.model_dump_json())
    if result.task_id != task.task_id or result.capability != task.capability:
        raise ValueError("执行结果与正式任务身份不一致")
    return result


async def task_execution(
    state: InvestigationState, *, investigation_agent: InvestigationAgent,
    execution_context: KnowledgeExecutionContext,
    authorize: Callable[[str, str, KnowledgeExecutionContext], bool],
    execute_phase,
    session_factory=None, embedding_client=None, milvus_client=None,
    before_task: Callable[[], object] | None = None,
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
    trusted = KnowledgeExecutionContext.model_validate_json(execution_context.model_dump_json())
    ids = [task.task_id for task in plan.tasks]
    if any(not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("正式任务 ID 必须非空且唯一")
    if not plan.tasks or any(task.status != TaskStatus.PENDING for task in plan.tasks):
        raise ValueError("首轮执行要求非空且全部待执行的正式任务")
    results, tasks = [], []
    for task in plan.tasks:
        if before_task is not None:
            before_task()

        async def query():
            started = datetime.now(timezone.utc)
            candidate, error = await formulate_task_query(
                task, planning_input, goal=plan.goal, investigation_agent=investigation_agent, trusted=trusted,
            )
            if error is not None:
                return QueryOutcome(failure=TaskExecutionResult(
                    execution_id="exec-" + uuid4().hex, task_id=task.task_id, capability=task.capability,
                    status=ExecutionStatus.FAILED, started_at=started, completed_at=datetime.now(timezone.utc), error=error,
                ))
            return QueryOutcome(candidate=candidate)

        formed = await phase(OperationType.QUERY_FORMULATION, query, task_id=task.task_id)
        if formed.failure is not None:
            result = formed.failure
        else:
            async def search():
                result = await execute_registered_tool(
                    KNOWLEDGE_SEARCH.name, task, formed.candidate, context=trusted, authorize=authorize, session_factory=session_factory,
                    embedding_client=embedding_client, milvus_client=milvus_client,
                )
                # 入账前校验身份，不能先保存错误结果再拒绝写 State。
                return _task_result(task, result)
            result = await phase(OperationType.KNOWLEDGE_EXECUTION, search,
                                 task_id=task.task_id, candidate=formed.candidate)
        result = _task_result(task, result)
        if any(previous.execution_id == result.execution_id for previous in results):
            raise ValueError("本轮执行记录 ID 不能重复")
        results.append(result)
        tasks.append(task.model_copy(update={"status": TaskStatus.COMPLETED
                     if result.status == ExecutionStatus.SUCCEEDED else TaskStatus.FAILED}))
    return {"investigation_plan": plan.model_copy(update={"tasks": tasks}),
            "task_execution_results": results}


async def observation(state: InvestigationState, *, investigation_agent: InvestigationAgent,
                      execution_context: KnowledgeExecutionContext, execute_phase) -> dict[str, object]:
    """统一 Observation 阶段入口，分离解释逻辑与持久化机制。"""
    plan = _business_model(InvestigationPlan, state["investigation_plan"])
    results = [_business_model(TaskExecutionResult, row) for row in state["task_execution_results"]]
    # 进入模型或持久化阶段前核验完整输入，不保存损坏的业务事实。
    plan, results = validate_round_inputs(plan, results)
    async def explain():
        update = await _interpret_observation(
            dict(investigation_plan=plan, task_execution_results=results),
            investigation_agent=investigation_agent, execution_context=execution_context,
        )
        return update["observation"]
    if not callable(execute_phase):
        raise TypeError("必须显式提供阶段执行器")
    output = await execute_phase(OperationType.OBSERVATION, explain, results=results)
    return {"observation": output}


async def _interpret_observation(
    state: InvestigationState, *, investigation_agent: InvestigationAgent,
    execution_context: KnowledgeExecutionContext,
) -> dict[str, object]:
    """先校验执行事实，再解释并验证来源。没有知识或解释失败时明确不确定。"""
    plan, results = validate_round_inputs(state["investigation_plan"], state["task_execution_results"])
    summaries, _ = summarize_executions(results)
    if not any(item.outcome == "KNOWLEDGE_AVAILABLE" for item in summaries):
        return {"observation": inconclusive_observation(
            results, "NO_KNOWLEDGE_AVAILABLE", "本轮没有可用于解释的知识，未调用解释模型",
        )}
    try:
        async with asyncio.timeout(execution_context.timeout_seconds):
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
