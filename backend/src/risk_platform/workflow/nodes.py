"""首轮知识执行节点；Observation 与 Replanning 仍未实现。"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from openai import APITimeoutError, APIConnectionError, APIStatusError
from pydantic import ValidationError

from risk_platform.agents.investigation import InvestigationAgent
from risk_platform.execution.contracts import (
    ErrorCategory, ExecutionError, ExecutionStatus, KnowledgeExecutionContext, TaskExecutionResult,
)
from risk_platform.execution.knowledge import execute_knowledge_search
from risk_platform.workflow.contracts import CapabilityType, InitialPlanningInput, InvestigationPlan, TaskStatus
from risk_platform.workflow.state import InvestigationState


def initial_planning(
    state: InvestigationState,
    *,
    investigation_agent: InvestigationAgent,
) -> dict[str, object]:
    """Ask the Investigation Agent what the first round should investigate."""

    plan = investigation_agent.initial_plan(state["planning_input"])

    # 新轮次清除旧的结束标记，案件保持开放。
    return {"investigation_plan": plan, "task_execution_results": [],
            "round_complete": False, "investigation_status": "OPEN"}


async def task_execution(
    state: InvestigationState, *, investigation_agent: InvestigationAgent,
    execution_context: KnowledgeExecutionContext,
    authorize: Callable[[str, str, KnowledgeExecutionContext], bool],
    session_factory=None, embedding_client=None, milvus_client=None,
) -> dict[str, object]:
    """按计划顺序构造 query 并调用 7A；普通任务失败不阻止后续独立任务。

    无自动重试或并发。取消传播；本节点完成后统一返回本轮结果，不提供断点续执行保证。
    """
    plan = InvestigationPlan.model_validate_json(state["investigation_plan"].model_dump_json())
    planning_input = InitialPlanningInput.model_validate_json(state["planning_input"].model_dump_json())
    trusted = KnowledgeExecutionContext.model_validate_json(execution_context.model_dump_json())
    ids = [task.task_id for task in plan.tasks]
    if any(not key.strip() for key in ids) or len(ids) != len(set(ids)):
        raise ValueError("正式任务 ID 必须非空且唯一")
    if not plan.tasks or any(task.status != TaskStatus.PENDING for task in plan.tasks):
        raise ValueError("首轮执行要求非空且全部待执行的正式任务，不提供恢复或重复执行语义")
    results = []
    tasks = []
    for task in plan.tasks:
        started = datetime.now(timezone.utc)
        try:
            if task.capability == CapabilityType.KNOWLEDGE_RETRIEVAL:
                # 现有 Agent 使用同步 SDK，移到线程避免阻塞事件循环；不把 objective 当作 query。
                async with asyncio.timeout(trusted.timeout_seconds):
                    candidate = await asyncio.to_thread(
                        investigation_agent.formulate_knowledge_query,
                        task.model_copy(deep=True), planning_input.model_copy(deep=True),
                        goal=plan.goal, max_top_k=trusted.max_top_k,
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
        if error is not None:
            # 模型失败也要有系统生成的尝试记录，但不伪造一次已发生的 RAG 调用。
            result = TaskExecutionResult(
                execution_id="exec-" + uuid4().hex, task_id=task.task_id, capability=task.capability,
                status=ExecutionStatus.FAILED, started_at=started, completed_at=datetime.now(timezone.utc), error=error,
            )
        else:
            result = await execute_knowledge_search(
                task, candidate, context=trusted, authorize=authorize, session_factory=session_factory,
                embedding_client=embedding_client, milvus_client=milvus_client,
            )
        result = TaskExecutionResult.model_validate_json(result.model_dump_json())
        if result.task_id != task.task_id or result.capability != task.capability:
            raise ValueError("执行结果与正式任务身份不一致")
        if any(previous.execution_id == result.execution_id for previous in results):
            raise ValueError("本轮执行记录 ID 不能重复")
        results.append(result)
        # 不修改输入计划对象。现有 TaskStatus 没有 REJECTED，拒绝仍映射 FAILED，细节留在结果中。
        tasks.append(task.model_copy(update={"status": TaskStatus.COMPLETED
                     if result.status == ExecutionStatus.SUCCEEDED else TaskStatus.FAILED}))
    return {"investigation_plan": plan.model_copy(update={"tasks": tasks}),
            "task_execution_results": results}


def observation(_: InvestigationState) -> dict[str, object]:
    """Run the V0 observation placeholder."""

    return {}


def replanning(_: InvestigationState) -> dict[str, object]:
    """结束本轮 V0 骨架遍历；不表示案件调查完成，也不执行真实重规划。"""

    # Temporary V0 skeleton behavior: real replanning will decide whether to
    # continue the investigation after later workflow chapters are implemented.
    return {"round_complete": True, "investigation_status": "OPEN"}
