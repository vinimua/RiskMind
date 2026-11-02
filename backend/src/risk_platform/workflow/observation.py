"""7C 的数据与确定性校验：解释不替代执行事实，历史知识不等于当前案件证据。"""

from datetime import datetime, timezone
from typing import Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from risk_platform.execution.contracts import ExecutionStatus, TaskExecutionResult
from risk_platform.knowledge.contracts import KnowledgeSourceRef, _NonBlankString
from risk_platform.workflow.contracts import InvestigationPlan, TaskStatus


class ObservationIntegrityError(ValueError):
    """本轮输入或解释引用不一致，不接受部分解释。"""


class ObservationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationSource(ObservationContract):
    execution_id: _NonBlankString
    context_id: _NonBlankString
    source: KnowledgeSourceRef
    quote: _NonBlankString  # 必须完整出现在所引用 Context 中，不拼接多份正文。


class KnowledgeFinding(ObservationContract):
    statement: _NonBlankString
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)
    sources: tuple[ObservationSource, ...] = Field(min_length=1)
    limitation: _NonBlankString


class ObservationNote(ObservationContract):
    text: _NonBlankString
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)


class KnowledgeObservationDraft(ObservationContract):
    """模型只能提出解释和引用，不能生成可信 observation_id、状态或执行事实。"""

    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)
    findings: tuple[KnowledgeFinding, ...]
    knowledge_gaps: tuple[ObservationNote, ...]
    next_step_suggestions: tuple[ObservationNote, ...]
    narrative: _NonBlankString


class ExecutionObservation(ObservationContract):
    """系统从真实执行记录推导；不会让模型把 FAILED 改写成零命中。"""

    execution_id: _NonBlankString
    task_id: _NonBlankString
    status: ExecutionStatus
    outcome: Literal["KNOWLEDGE_AVAILABLE", "NO_HITS", "EXECUTION_FAILED", "EXECUTION_REJECTED"]


class ObservationError(ObservationContract):
    code: _NonBlankString
    message: _NonBlankString


class RoundObservation(ObservationContract):
    observation_id: _NonBlankString
    created_at: AwareDatetime
    status: Literal["SUCCEEDED", "INCONCLUSIVE"]
    # 系统固定认知边界，没有当前根因、证据准入或案件关闭字段。
    knowledge_scope: Literal["HISTORICAL_KNOWLEDGE_ONLY"] = "HISTORICAL_KNOWLEDGE_ONLY"
    execution_refs: tuple[_NonBlankString, ...] = Field(min_length=1)
    execution_summaries: tuple[ExecutionObservation, ...] = Field(min_length=1)
    findings: tuple[KnowledgeFinding, ...] = ()
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
        if result.payload is not None:
            context_ids = [context.context_id for context in result.payload.resolved.contexts]
            if len(context_ids) != len(set(context_ids)):
                raise ObservationIntegrityError("同一次执行的上下文 ID 不能重复")
    return plan, results


def summarize_executions(results):
    summaries, gaps = [], []
    for result in results:
        if result.status == ExecutionStatus.SUCCEEDED:
            outcome = "KNOWLEDGE_AVAILABLE" if result.payload.resolved.contexts else "NO_HITS"
        else:
            outcome = "EXECUTION_REJECTED" if result.status == ExecutionStatus.REJECTED else "EXECUTION_FAILED"
        summaries.append(ExecutionObservation(execution_id=result.execution_id, task_id=result.task_id,
                                              status=result.status, outcome=outcome))
        if outcome != "KNOWLEDGE_AVAILABLE":
            text = {"NO_HITS": "知识检索正常完成但没有命中，不代表不存在相关知识。",
                    "EXECUTION_REJECTED": "知识执行被拒绝，本任务尚未完成知识查询。",
                    "EXECUTION_FAILED": "本任务执行失败，不能据此判断没有类似案例。"}[outcome]
            gaps.append(ObservationNote(text=text, execution_refs=(result.execution_id,)))
    return tuple(summaries), tuple(gaps)


def build_observation_input(plan, results):
    """只向解释模型提供本轮任务与实际知识/错误，不提供授权器和操作者身份。"""
    return {
        "goal": plan.goal,
        "tasks": [{"task_id": t.task_id, "objective": t.objective, "expected_result": t.expected_result}
                  for t in plan.tasks],
        "executions": [r.model_dump(mode="json") for r in results],
        "output_schema": KnowledgeObservationDraft.model_json_schema(),
    }


def validate_observation_draft(draft: KnowledgeObservationDraft, results):
    """机械验证引用存在和来源归属；不声称可证明自然语言陈述的语义蕴含。"""
    draft = KnowledgeObservationDraft.model_validate_json(draft.model_dump_json())
    by_id = {r.execution_id: r for r in results}

    def check_refs(refs):
        if len(refs) != len(set(refs)) or not set(refs) <= set(by_id):
            raise ObservationIntegrityError("解释引用必须唯一且属于当前轮次")

    check_refs(draft.execution_refs)
    if set(draft.execution_refs) != set(by_id):
        raise ObservationIntegrityError("解释必须声明当前轮次全部执行记录，不能隐去失败")
    for finding in draft.findings:
        check_refs(finding.execution_refs)
        if set(finding.execution_refs) != {s.execution_id for s in finding.sources}:
            raise ObservationIntegrityError("发现的执行引用必须与来源引用对应")
        seen_sources = set()
        for source in finding.sources:
            result = by_id[source.execution_id]
            if result.status != ExecutionStatus.SUCCEEDED:
                raise ObservationIntegrityError("失败或拒绝执行不能支持历史知识发现")
            contexts = [c for c in result.payload.resolved.contexts if c.context_id == source.context_id]
            if len(contexts) != 1:
                raise ObservationIntegrityError("发现引用的 Context 不属于对应执行结果")
            context = contexts[0]
            if source.source not in [hit.source for hit in context.supporting_hits] or source.quote not in context.content:
                raise ObservationIntegrityError("发现的 Child 来源或原文引句与对应 Context 不一致")
            key = (source.execution_id, source.context_id, source.source.model_dump_json(), source.quote)
            if key in seen_sources:
                raise ObservationIntegrityError("发现中的来源引用不能重复")
            seen_sources.add(key)
    for note in (*draft.knowledge_gaps, *draft.next_step_suggestions):
        check_refs(note.execution_refs)
    return draft


def complete_observation(results, draft):
    draft = validate_observation_draft(draft, results)
    summaries, gaps = summarize_executions(results)
    return RoundObservation(
        observation_id="obs-" + uuid4().hex, created_at=datetime.now(timezone.utc), status="SUCCEEDED",
        execution_refs=tuple(r.execution_id for r in results), execution_summaries=summaries,
        findings=draft.findings, knowledge_gaps=gaps + draft.knowledge_gaps,
        next_step_suggestions=draft.next_step_suggestions, narrative=draft.narrative,
    )


def inconclusive_observation(results, code, message):
    summaries, gaps = summarize_executions(results)
    return RoundObservation(
        observation_id="obs-" + uuid4().hex, created_at=datetime.now(timezone.utc), status="INCONCLUSIVE",
        execution_refs=tuple(r.execution_id for r in results), execution_summaries=summaries,
        knowledge_gaps=gaps, narrative="本轮尚未形成通过校验的知识解释，不能据此确认当前案件根因。",
        error=ObservationError(code=code, message=message),
    )
