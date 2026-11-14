"""7C 的数据与确定性校验：解释不替代执行事实，历史知识不等于当前案件证据。"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4
import json

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from risk_platform.execution.contracts import ExecutionStatus, TaskExecutionResult, KnowledgeExecutionPayload
from risk_platform.knowledge.contracts import KnowledgeSourceRef, _NonBlankString
from risk_platform.workflow.contracts import InvestigationPlan, TaskStatus
from risk_platform.core.token_budget import ContextBudgetExceeded, estimate_tokens

# 工具观察区的输入预算，不是模型的总窗口；协议摘要也记录此值。
TOOL_OBSERVATION_TOKENS = 8192
TOOL_CONTEXT_TOKENS = 2048


class ObservationIntegrityError(ValueError):
    """本轮输入或解释引用不一致，不接受部分解释。"""


class ObservationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationSource(ObservationContract):
    execution_id: _NonBlankString
    context_id: _NonBlankString
    source: KnowledgeSourceRef
    quote: _NonBlankString  # 必须完整出现在所引用 Context 中，不拼接多份正文。


class ToolObservationSource(ObservationContract):
    execution_id: _NonBlankString
    reference: dict  # 由该执行工具注册的 reference_model 和校验函数验证。


class LegacyFinding(ObservationContract):
    statement: _NonBlankString
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)
    sources: tuple[ObservationSource, ...] = Field(min_length=1)
    limitation: _NonBlankString


class ObservationNote(ObservationContract):
    text: _NonBlankString
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)


class Finding(ObservationContract):
    statement: _NonBlankString = Field(max_length=4000)
    sources: tuple[ObservationSource | ToolObservationSource, ...] = Field(min_length=1, max_length=32)


class ObservationDraft(ObservationContract):
    """模型提出解释和推进建议，不填写系统身份或重复的执行 ID。"""
    findings: tuple[Finding, ...] = Field(max_length=64)
    unresolved_questions: tuple[_NonBlankString, ...] = Field(max_length=64)
    action: Literal["CONTINUE", "STOP"]
    stop_reason: _NonBlankString | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        questions = [q.strip() for q in self.unresolved_questions]
        if len(questions) != len(set(questions)) or any(len(q) > 2000 for q in questions):
            raise ValueError("未解决问题不能重复或超过长度限制")
        if self.action == "CONTINUE" and (not questions or self.stop_reason is not None):
            raise ValueError("继续必须有未解决问题，且不能包含停止原因")
        if self.action == "STOP" and self.stop_reason is None:
            raise ValueError("停止必须说明原因")
        return self


class ExecutionObservation(ObservationContract):
    """系统从真实执行记录推导；不会让模型把 FAILED 改写成零命中。"""

    execution_id: _NonBlankString
    task_id: _NonBlankString
    status: ExecutionStatus
    outcome: Literal["KNOWLEDGE_AVAILABLE", "NO_HITS", "RESULT_AVAILABLE", "EMPTY_RESULT", "EXECUTION_FAILED", "EXECUTION_REJECTED"]


class ObservationError(ObservationContract):
    code: _NonBlankString
    message: _NonBlankString


class LegacyRoundObservation(ObservationContract):
    observation_id: _NonBlankString
    created_at: AwareDatetime
    status: Literal["SUCCEEDED", "INCONCLUSIVE"]
    # 系统固定认知边界，没有当前根因、证据准入或案件关闭字段。
    knowledge_scope: Literal["HISTORICAL_KNOWLEDGE_ONLY"] = "HISTORICAL_KNOWLEDGE_ONLY"
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)
    execution_summaries: tuple[ExecutionObservation, ...] = Field(min_length=1)
    findings: tuple[LegacyFinding, ...] = ()
    knowledge_gaps: tuple[ObservationNote, ...] = ()
    next_step_suggestions: tuple[ObservationNote, ...] = ()
    narrative: _NonBlankString
    error: ObservationError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "SUCCEEDED" and self.error is not None:
            raise ValueError("成功解释不能包含错误")
        if self.status == "INCONCLUSIVE" and (
            self.error is None or self.findings or self.next_step_suggestions
        ):
            raise ValueError("不确定解释必须说明原因，不能保留部分模型发现或建议")
        ids = [summary.execution_id for summary in self.execution_summaries]
        if len(ids) != len(set(ids)) or tuple(ids) != self.execution_refs:
            raise ValueError("系统执行摘要必须与本轮执行引用逐项对应")
        return self


class RoundObservation(ObservationDraft):
    """v2 正式解释；action 是建议，不授予下一轮运行权。"""
    schema_version: Literal[2] = 2
    observation_id: _NonBlankString
    created_at: AwareDatetime
    status: Literal["SUCCEEDED", "INCONCLUSIVE"]
    knowledge_scope: Literal["HISTORICAL_KNOWLEDGE_ONLY", "TOOL_RESULTS_ONLY"] = "HISTORICAL_KNOWLEDGE_ONLY"
    execution_summaries: tuple[ExecutionObservation, ...] = Field(min_length=1)
    error: ObservationError | None = None

    @property
    def execution_refs(self):
        # 完整执行集合由系统摘要推导，不要求模型重复填写。
        return tuple(item.execution_id for item in self.execution_summaries)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if len(set(self.execution_refs)) != len(self.execution_refs):
            raise ValueError("执行摘要身份不能重复")
        if self.status == "SUCCEEDED" and self.error is not None:
            raise ValueError("成功解释不能包含错误")
        if self.status == "INCONCLUSIVE" and (
            self.error is None or self.findings or self.action != "STOP"
        ):
            raise ValueError("解释失败必须停止，不能保留部分发现")
        return self


def validate_round_inputs(plan: InvestigationPlan, results: list[TaskExecutionResult]):
    """验证本轮任务/结果一一关联及结束状态，返回重验后的独立副本。"""
    plan = InvestigationPlan.model_validate_json(plan.model_dump_json())
    results = [TaskExecutionResult.model_validate_json(r.model_dump_json()) for r in results]
    task_ids = [t.task_id for t in plan.tasks]
    execution_ids = [r.execution_id for r in results]
    if (not task_ids or any(not key.strip() for key in task_ids)
            or len(task_ids) != len(set(task_ids)) or len(execution_ids) != len(set(execution_ids))
            or [r.task_id for r in results] != task_ids):
        raise ObservationIntegrityError("本轮正式任务与执行结果必须按顺序一一关联且身份唯一")
    for task, result in zip(plan.tasks, results, strict=True):
        expected = TaskStatus.COMPLETED if result.status == ExecutionStatus.SUCCEEDED else TaskStatus.FAILED
        if task.capability != result.capability or task.status != expected:
            raise ObservationIntegrityError("正式任务能力或状态与执行事实不一致")
        if isinstance(result.payload, KnowledgeExecutionPayload):
            context_ids = [context.context_id for context in result.payload.resolved.contexts]
            if len(context_ids) != len(set(context_ids)):
                raise ObservationIntegrityError("同一次执行的上下文 ID 不能重复")
    return plan, results


def summarize_executions(results):
    summaries, gaps = [], []
    for result in results:
        if result.status == ExecutionStatus.SUCCEEDED:
            from risk_platform.execution.tools import tool_definition
            definition = tool_definition(result.tool_name or "knowledge_search")
            payload = definition.result_model.model_validate(result.payload.model_dump() if isinstance(result.payload, BaseModel) else result.payload)
            items = definition.to_observation_items(payload)
            outcome = ("KNOWLEDGE_AVAILABLE" if items else "NO_HITS") if result.tool_name is None else ("RESULT_AVAILABLE" if items else "EMPTY_RESULT")
        else:
            outcome = "EXECUTION_REJECTED" if result.status == ExecutionStatus.REJECTED else "EXECUTION_FAILED"
        summaries.append(ExecutionObservation(execution_id=result.execution_id, task_id=result.task_id,
                                              status=result.status, outcome=outcome))
        if outcome not in ("KNOWLEDGE_AVAILABLE", "RESULT_AVAILABLE"):
            text = {"NO_HITS": "知识检索正常完成但没有命中，不代表不存在相关知识。",
                    "EXECUTION_REJECTED": "知识执行被拒绝，本任务尚未完成知识查询。",
                    "EXECUTION_FAILED": "本任务执行失败，不能据此判断没有类似案例。",
                    "EMPTY_RESULT": "工具正常完成但没有可解释结果。"}[outcome]
            gaps.append(ObservationNote(text=text, execution_refs=(result.execution_id,)))
    return tuple(summaries), tuple(gaps)


def build_observation_input(plan, results, *, planning_input=None, previous_observations=(), available_capabilities=()):
    """只向解释模型提供本轮任务与实际知识/错误，不提供授权器和操作者身份。"""
    return {
        "original_input": planning_input.model_dump(mode="json") if planning_input is not None else None,
        "previous_observations": [o.model_dump(mode="json") for o in previous_observations],
        "available_capabilities": list(available_capabilities),
        "goal": plan.goal,
        "tasks": [{"task_id": t.task_id, "objective": t.objective, "expected_result": t.expected_result}
                  for t in plan.tasks],
        "executions": project_tool_results(results),
        "output_schema": ObservationDraft.model_json_schema(),
    }


# max_tokens=8192,       # 整份工具结果区的预算
# context_tokens=2048,   # 每个知识片段的预算
# `project_tool_results` 意思是：**从完整工具结果中，选出本次要发给模型的内容。**
# ```text
# 完整工具结果 results
#   包含状态、时间、请求、索引信息、全部知识正文……
#     ↓
# project_tool_results()
#   整理字段，按 token 预算选择正文
#     ↓
# 给 Observation 模型看的执行结果列表
# ```
# 它**不执行工具、不生成结论、不修改数据库结果**。

def project_tool_results(results, *, max_tokens=TOOL_OBSERVATION_TOKENS,
                         context_tokens=TOOL_CONTEXT_TOKENS):
    """给解释模型的结果投影；不修改执行事实、不生成第二份持久化结果。
把完整工具结果整理成一份 token 预算内、供 Observation 模型阅读的数据副本。
    所有执行状态必须保留。正文按工具提供的完整结果项选择，来源与正文一起保留，
    不按字符截断引文；省略只表示预算限制，不能被模型解释成零命中。
    """
    if type(max_tokens) is not int or type(context_tokens) is not int or min(max_tokens, context_tokens) <= 0:
        raise ValueError("工具观察 token 预算必须为正整数")

    def cost(value):
        return estimate_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    from risk_platform.execution.tools import tool_definition
    projected, candidates = [], []
    summaries, _ = summarize_executions(results)
    for result, summary in zip(results, summaries, strict=True):
        item = dict(execution_id=result.execution_id, task_id=result.task_id,
                    tool_name=result.tool_name or "knowledge_search", status=result.status.value,
                    outcome=summary.outcome, items=[], omitted_items=0)
        units = []
        if result.payload is not None:
            definition = tool_definition(item["tool_name"])
            payload = definition.result_model.model_validate(
                result.payload.model_dump() if isinstance(result.payload, BaseModel) else result.payload)
            units = definition.to_observation_items(payload)
            ids = [u["reference_id"] for u in units]
            if any(not isinstance(identity, str) or not identity.strip() for identity in ids) or len(set(ids)) != len(ids):
                raise ObservationIntegrityError("Duplicate result item identities")
            item["omitted_items"] = len(units)
            item["reference_schema"] = definition.reference_model.model_json_schema()
        elif result.error is not None:
            item["error"] = result.error.model_dump(mode="json")
        projected.append(item)
        candidates.append(units)
    if cost(projected) > max_tokens:
        raise ContextBudgetExceeded("Execution metadata exceeds token budget")
    for units, item in zip(candidates, projected, strict=True):
        for unit in units:
            if cost(unit) > context_tokens:
                continue
            item["items"].append(unit)
            item["omitted_items"] -= 1
            if cost(projected) > max_tokens:
                item["items"].pop()
                item["omitted_items"] += 1
    return projected
# 这几个是 Observation 输入中 executions 每一项的内部字段

def validate_observation_draft(draft: ObservationDraft, results):
    """机械验证引用存在和来源归属；不声称可证明自然语言陈述的语义蕴含。"""
    draft = ObservationDraft.model_validate_json(draft.model_dump_json())
    by_id = {r.execution_id: r for r in results}

    for finding in draft.findings:
        seen_sources = set()
        for source in finding.sources:
            if source.execution_id not in by_id:
                raise ObservationIntegrityError("发现引用不属于当前轮次")
            result = by_id[source.execution_id]
            if result.status != ExecutionStatus.SUCCEEDED:
                raise ObservationIntegrityError("失败或拒绝执行不能支持发现")
            from risk_platform.execution.tools import tool_definition
            definition = tool_definition(result.tool_name or "knowledge_search")
            reference = source.reference if isinstance(source, ToolObservationSource) else source.model_dump(
                mode="json", exclude={"execution_id"})
            payload = definition.result_model.model_validate(
                result.payload.model_dump() if isinstance(result.payload, BaseModel) else result.payload)
            try:
                definition.reference_model.model_validate(reference)
                definition.validate_reference(reference, payload)
            except ValueError as error:
                raise ObservationIntegrityError("Result reference is not supported by execution") from error
            key = (source.execution_id, json.dumps(reference, sort_keys=True))
            if key in seen_sources:
                raise ObservationIntegrityError("发现中的来源引用不能重复")
            seen_sources.add(key)
    return draft


def complete_observation(results, draft):
    draft = validate_observation_draft(draft, results)
    summaries, _ = summarize_executions(results)
    return RoundObservation(
        observation_id="obs-" + uuid4().hex, created_at=datetime.now(timezone.utc), status="SUCCEEDED",
        execution_summaries=summaries, knowledge_scope="TOOL_RESULTS_ONLY" if any(r.tool_name for r in results) else "HISTORICAL_KNOWLEDGE_ONLY", **draft.model_dump(),
    )


def inconclusive_observation(results, code, message):
    summaries, _ = summarize_executions(results)
    return RoundObservation(
        observation_id="obs-" + uuid4().hex, created_at=datetime.now(timezone.utc), status="INCONCLUSIVE",
        execution_summaries=summaries, findings=(), unresolved_questions=(), action="STOP",
        stop_reason=message, error=ObservationError(code=code, message=message),
    )


def read_observation(value):
    """仅历史回查接受旧格式，不为旧记录伪造推进建议。"""
    contract = RoundObservation if value.get("schema_version") == 2 else LegacyRoundObservation
    return contract.model_validate(value)
