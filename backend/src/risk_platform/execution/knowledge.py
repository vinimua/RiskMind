"""唯一知识工具的受控适配器，不建立通用 Registry/Executor 框架。"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError
from pymilvus.exceptions import MilvusException
from sqlalchemy.exc import SQLAlchemyError

from risk_platform.execution.contracts import (
    ErrorCategory, ExecutionError, ExecutionStatus, KnowledgeExecutionContext,
    KnowledgeExecutionPayload, KnowledgeSearchToolRequest, TaskExecutionResult,
)
from risk_platform.knowledge.context_resolution import ContextResolutionIntegrityError, resolve_knowledge_contexts
from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.retrieval import ChildRetrievalIntegrityError, retrieve_dense_children
from risk_platform.knowledge.serving import MissingServingRouteError
from risk_platform.workflow.contracts import CapabilityType, InvestigationTask


def _scoped_request(candidate, context: KnowledgeExecutionContext) -> KnowledgeSearchToolRequest:
    """校验候选，再收紧范围；不接受候选覆盖可信应用的限定。"""
    data = candidate.model_dump(mode="json") if isinstance(candidate, KnowledgeSearchToolRequest) else candidate
    request = KnowledgeSearchToolRequest.model_validate(data)
    if request.top_k > context.max_top_k:
        raise ValueError("请求数量超过系统上限")
    filters = request.filters.model_dump(mode="json")
    for key in ("document_id", "document_version", "model_id"):
        required = getattr(context.scope, key)
        if required is not None:
            if filters[key] is not None and filters[key] != required:
                raise ValueError("候选请求超出系统范围")
            filters[key] = required
    allowed = context.scope.document_types
    if allowed:
        if request.filters.document_types and not set(request.filters.document_types) <= set(allowed):
            raise ValueError("候选文档类型超出系统范围")
        filters["document_types"] = request.filters.document_types or allowed
    return KnowledgeSearchToolRequest(query=request.query, top_k=request.top_k,
                                      filters=KnowledgeFilters.model_validate(filters))


async def execute_knowledge_search(
    task: InvestigationTask, candidate_request, *, context: KnowledgeExecutionContext,
    authorize: Callable[[str, str, KnowledgeExecutionContext], bool],
    session_factory=None, embedding_client=None, milvus_client=None,
) -> TaskExecutionResult:
    """可信调用方提供 Task/context/鉴权器；模型只提供候选 query/filters/top_k。

    不自动重试、不更新任务状态、不落库；一次调用分配一次系统 execution_id。
    取消请求继续向上传播，不把取消当作正常执行结果。
    """
    # 系统输入非法属于调用方编程错误，不能靠虚构 task_id 生成可审计执行记录。
    task = InvestigationTask.model_validate_json(task.model_dump_json())
    context = KnowledgeExecutionContext.model_validate_json(context.model_dump_json())
    if not task.task_id.strip():
        raise ValueError("可信任务 ID 不能为空")
    execution_id = "exec-" + uuid4().hex
    started = datetime.now(timezone.utc)

    def finish(status, *, payload=None, category=None, code=None, message=None, retryable=False):
        error = None if category is None else ExecutionError(
            category=category, code=code, message=message, retryable=retryable,
        )
        return TaskExecutionResult(execution_id=execution_id, task_id=task.task_id,
                                   capability=task.capability, status=status, started_at=started,
                                   completed_at=datetime.now(timezone.utc), payload=payload, error=error)

    if task.capability != CapabilityType.KNOWLEDGE_RETRIEVAL:
        return finish(ExecutionStatus.REJECTED, category=ErrorCategory.VALIDATION,
                      code="UNSUPPORTED_CAPABILITY", message="本入口只允许知识检索任务")
    try:
        request = _scoped_request(candidate_request, context)
    except (ValueError, TypeError):
        return finish(ExecutionStatus.REJECTED, category=ErrorCategory.VALIDATION,
                      code="INVALID_TOOL_REQUEST", message="知识请求非法或超出系统允许范围")
    try:
        # 只有可信鉴权函数的显式 True 才放行，不能根据模型字段或真值对象放行。
        if authorize(context.actor_id, "knowledge:search", context) is not True:
            return finish(ExecutionStatus.REJECTED, category=ErrorCategory.AUTHORIZATION,
                          code="TOOL_NOT_AUTHORIZED", message="当前身份无权执行知识检索")
    except Exception:
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.INTERNAL,
                      code="AUTHORIZATION_CHECK_FAILED", message="权限检查未能完成")

    try:
        # 覆盖检索与解析全链路；不包括输入校验与同步鉴权，不配置第二层自动重试。
        async with asyncio.timeout(context.timeout_seconds):
            retrieved = await retrieve_dense_children(
                request, index_profile=context.index_profile, session_factory=session_factory,
                embedding_client=embedding_client, milvus_client=milvus_client,
            )
            resolved = await resolve_knowledge_contexts(retrieved, session_factory=session_factory)
            payload = KnowledgeExecutionPayload(request=request, retrieved=retrieved, resolved=resolved)
            if retrieved.snapshot.index_profile != context.index_profile:
                raise ChildRetrievalIntegrityError("结果策略与系统选择不一致")
        return finish(ExecutionStatus.SUCCEEDED, payload=payload)
    except (TimeoutError, APITimeoutError):
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.DEPENDENCY,
                      code="KNOWLEDGE_SEARCH_TIMEOUT", message="知识检索超时，未获得完整结果", retryable=True)
    except (ChildRetrievalIntegrityError, ContextResolutionIntegrityError, ValidationError):
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.INTEGRITY,
                      code="KNOWLEDGE_INTEGRITY_FAILURE", message="知识结果或上下文来源校验失败")
    except MissingServingRouteError:
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.DEPENDENCY,
                      code="SERVING_ROUTE_MISSING", message="所选索引策略尚未配置服务路由")
    except APIStatusError as error:
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.DEPENDENCY,
                      code="EMBEDDING_API_FAILURE", message="向量服务请求失败",
                      retryable=error.status_code == 429 or error.status_code >= 500)
    except (APIConnectionError, SQLAlchemyError, MilvusException, ConnectionError):
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.DEPENDENCY,
                      code="KNOWLEDGE_DEPENDENCY_FAILURE", message="知识检索依赖未能完成请求", retryable=True)
    except Exception:
        # 不返回原异常正文，防止把 DSN、API Key 或上游响应暴露给 Agent/日志消费者。
        return finish(ExecutionStatus.FAILED, category=ErrorCategory.INTERNAL,
                      code="KNOWLEDGE_EXECUTION_FAILURE", message="知识检索执行未能完成")
