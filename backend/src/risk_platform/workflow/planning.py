"""图内计划准备：生成候选、确定性校验、接受入账，或加载已接受计划。"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from sqlalchemy import select
from risk_platform.workflow import access
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence.contracts import RoundProgression, RoundRecord, OperationType, operation_identity_key
from risk_platform.workflow.persistence.models import RoundProgressionRow, RoundRow, ResultRow, OperationRow
from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, read_run_identity
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow, InvestigationRunSelectionRow
from risk_platform.workflow.persistence.business_inputs import business_projection
from risk_platform.workflow.run_guard import RunExecutionContext, RunGuard
from risk_platform.workflow.observation import read_observation
from risk_platform.agents.prompts import INITIAL_PLANNING_SYSTEM_PROMPT
from risk_platform.memory.repository import context_for_stage
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import AcceptedPlan, input_digest
from risk_platform.workflow.persistence.models import AcceptedPlanRow
from risk_platform.workflow.persistence.planning_contracts import PlanningCandidate, PlanningInput
from risk_platform.workflow.persistence.planning_models import PlanningAcceptanceRow
from risk_platform.workflow.persistence import planning_repository as planning
from risk_platform.workflow.access import authorize_run
from risk_platform.workflow.run_guard import RecoveryBlockedError, _bind_execution_run
from risk_platform.workflow.checkpoints import to_checkpoint_data


class PlanningPreparationError(RuntimeError):
    """初始候选无法可靠产生或未通过确定性校验，未执行知识任务。"""


async def read_planning_dialogue(session, run_id):
    """读取只追加的方法记录；授权边界由调用方负责。"""
    from .persistence.planning_models import PlanningDialogueRow
    rows = list(await session.scalars(select(PlanningDialogueRow).where(
        PlanningDialogueRow.run_id == run_id).order_by(PlanningDialogueRow.sequence)))
    events = []
    for sequence, row in enumerate(rows):
        if row.sequence != sequence or hashlib.sha256(row.payload.encode()).hexdigest() != row.input_digest:
            raise ledger.LedgerIntegrityError("Planning dialogue sequence or digest differs")
        events.append(json.loads(row.payload))
    return events


def planning_methods(events):
    """从工具回复提取去重正文，不维护第二份持久化副本。"""
    materials = {}
    seen = set()
    for event in events:
        if event["kind"] == "read":
            for message in event["messages"]:
                value = json.loads(message["content"])
                if "content" in value:
                    key = (value["name"], value["resource"])
                    start = value.get("start_paragraph", 0)
                    page_key = (*key, value.get("digest"), start)
                    if page_key in seen:
                        continue
                    seen.add(page_key)
                    if start == 0:
                        materials[key] = value
                    else:
                        prior = materials.get(key)
                        if prior is None or prior.get("next_paragraph") != start or prior["digest"] != value["digest"]:
                            raise ledger.LedgerIntegrityError("方法分页不连续或版本变化")
                        materials[key] = prior | dict(content=prior["content"]+"\n\n"+value["content"],
                            next_paragraph=value["next_paragraph"])
    return list(materials.values())


def planning_messages(events):
    """保存压缩边界、恢复上下文，支持补读 Skill 和已提交执行结果。按已提交边界重建提示副本；原始读取记录始终保留。"""
    messages = [m for event in events if event["kind"] in {"model", "read"}
                for m in event["messages"]]
    context = next((e for e in reversed(events) if e["kind"] == "context"), None)
    if context is None:
        return messages
    messages = messages[context["through"]:]
    omitted = set(context["omitted_calls"])
    projected = []
    if context.get("summary"):
        projected.append(dict(role="user", content="较早规划交互的有损摘要，不是案件证据：\n"+context["summary"]))
    for message in messages:
        if message.get("tool_call_id") in omitted:
            value = json.loads(message["content"])
            value.pop("content", None)
            value.pop("items", None)
            value.update(body_omitted=True, recovery="需要原文时使用原读取工具补读同一来源")
            message = dict(message, content=json.dumps(value, ensure_ascii=False))
        projected.append(message)
    return projected



async def append_planning_event(session, run_id, events, event):
    """在调用方的短事务内追加事实；运行权与授权由调用方负责。"""
    from .persistence.planning_models import PlanningDialogueRow
    if await read_planning_dialogue(session, run_id) != events:
        raise ledger.LedgerConflictError("规划对话在当前步骤中发生变化")
    payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    session.add(PlanningDialogueRow(run_id=run_id, sequence=len(events), schema_version=1,
        payload=payload, input_digest=hashlib.sha256(payload.encode()).hexdigest()))


def select_task_methods(tasks, materials):
    """核对任务引用并投影已加载正文，不修改或复制持久化材料。"""
    available = {(m["name"], m["resource"]): m for m in materials}
    selected = {}
    for task in tasks:
        keys = [(ref.skill_name, ref.resource) for ref in task.skill_refs]
        if len(set(keys)) != len(keys):
            raise ValueError("同一任务不能重复引用方法文件")
        for key in keys:
            if key not in available:
                raise ValueError("任务引用了本轮未读取的方法文件")
            if available[key].get("next_paragraph") is not None:
                raise ValueError("任务引用的方法尚未完整读取")
            selected[key] = available[key]
    return tuple(selected.values())


def check_planning_retries(facts, policy):
    """只有明确授权的恢复策略才能重试失败或不确定的尝试。"""
    if any(row.status == "FAILED" for row in facts) and not policy.retry_failed_attempts:
        raise RecoveryBlockedError("规划曾失败，当前策略不允许重试")
    if any(row.status == "UNCERTAIN" for row in facts) and not policy.retry_uncertain:
        raise RecoveryBlockedError("规划存在不确定尝试，当前策略不允许重试")


async def settle_planning_failure(sessions, invocation, runtime, error):
    """结算当前尝试，保留已提交材料；进程丢失由下次领取处理。"""
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.identity.run_id)
        active = next((row for row in facts if row.status == "STARTED"), None)
        if active is not None:
            invalid = isinstance(error, ValueError)
            await planning.settle(session, active.attempt_id,
                status="FAILED" if invalid else "UNCERTAIN",
                code="INITIAL_PLANNING_INVALID" if invalid else "INITIAL_PLANNING_UNCERTAIN")
    raise PlanningPreparationError("规划未可靠完成，未接受计划、未执行任务") from error


async def prepare_plan(invocation, *, sessions, agent, runtime):
    """完整规划入口：准备上下文、按需读取、生成候选并正式接受计划。"""
    if invocation.guard.generation:
        return (await load_accepted_plan(invocation, sessions=sessions, runtime=runtime)) | {
            "planning_next": "task_execution"}
    # 生成阶段，从固定输入读取
    source = runtime.investigation.planning_input
    #claim_id 标识当前执行者这次持有的运行权，用于区分正常继续与进程中断后的重新领取。
    claim_id = "planning-claim-" + invocation.guard.owner_id
    async with sessions.begin() as session:
#         这几行依次取得：
# - material：之前查过什么，还有哪些问题。
# - spec：这轮使用哪个模型、什么调查输入和 Prompt 指纹。
# - facts：候选计划的生成尝试是否完成。
# - events：模型已经读到了哪些 Skill 内容。
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        material = await round_material(session, invocation, runtime)
        spec = planning_specification(invocation, runtime, agent, material)
        await planning.register(session, spec)
        facts = await planning.history(session, invocation.identity.run_id)
        committed = any(row.status == "COMMITTED" for row in facts)
        # 读取本轮已经保存的规划过程。
        events = await read_planning_dialogue(session, invocation.identity.run_id)
        if not committed:
            check_planning_retries(facts, invocation.policy)
            # 情况一：第一次进入，建立目录
            if not events:
                snapshot = dict(kind="catalogue", skills=agent.skill_directory.snapshot(),
                    deadline=datetime.now(timezone.utc).timestamp() + runtime.timeout_seconds,
                    max_reads=8, method_tokens=12000, model=agent.model)
                await append_planning_event(session, invocation.identity.run_id, events, snapshot)
                events.append(snapshot)
            # 情况二：上次已经保存了读取请求，还没读完
            if events[-1]["kind"] == "model":
                return {"planning_next": "read_skill"}
            # 情况三：上次调用模型时进程中断了
            active = next((row for row in facts if row.status == "STARTED"), None)
            if active is not None and active.claim_id != claim_id and events[-1]["kind"] == "calling":
                await planning.settle(session, active.attempt_id, status="UNCERTAIN", code="PREVIOUS_PLANNING_INTERRUPTED")
# 模型生成候选
#     ↓
# 候选保存成功 → committed=True
#     ↓
# 校验并正式接受
#     ↓
# 执行任务
    if not committed:
        # 不确定事实先提交；重试策略拒绝时也不能回滚掉它。
        async with sessions.begin() as session:
# - authorize_run()：当前用户是否有权读取、继续这个案件，返回核验后的信息 trusted。
# - check_lock()：当前执行者是否仍持有运行权，防止两个执行者同时推进。
            trusted = await authorize_run(session, runtime)
            await invocation.guard.check_lock()
#             facts 是本轮之前的尝试记录。
# check_planning_retries() 检查：之前失败或不确定的尝试，当前策略是否允许重试。
# next(..., None) 找第一条 STARTED 记录；没有就得到 None。
# | 场景 | `facts` 中的状态 | 怎么处理 |
# |---|---|---|
# | 第一次生成计划 | 没有记录 | `active=None`，创建第一次尝试 |
# | 模型请求 Skill，读取后回来继续 | 原尝试仍是 `STARTED` | 找到 `active`，沿用同一尝试 |
# | 模型返回了非法计划 | 原尝试为 `FAILED` | 策略允许才创建新尝试，否则停止 |
# | 模型调用超时或调用期间进程崩溃 | 尝试被标记为 `UNCERTAIN` | 策略允许才重新调用 |
# | 候选已可靠保存 | 有 `COMMITTED` | 跳过这段，直接正式接受 |
            facts = await planning.history(session, invocation.identity.run_id)
            check_planning_retries(facts, invocation.policy)
            active = next((row for row in facts if row.status == "STARTED"), None)
            # planning.start() 会写入一条状态为 STARTED 的尝试记录，记录本轮输入、执行人、领取标识和尝试序号。
            attempt_id = active.attempt_id if active is not None else await planning.start(
                session, spec, context=runtime, trusted=trusted, claim_id=claim_id, number=len(facts) + 1)
            memory = await context_for_stage(session, runtime=runtime,
                run_id=invocation.identity.run_id, stage="PLANNING")
            calling = dict(kind="calling", attempt_id=attempt_id, actor_id=trusted.actor_id)
            await append_planning_event(session, invocation.identity.run_id, events, calling)
        events.append(calling)
#  数据库：
#   一条 STARTED 尝试
#   一条 calling 事件

# 内存：
#   attempt_id：本次尝试编号
#   memory：准备给模型的历史记忆
#   events：已包含 calling 标记

# 下一步：
#   才调用 agent.initial_plan(...)
        snapshot = events[0]
        try:
            if snapshot["model"] != agent.model:
                raise ValueError("规划模型与固定输入不一致")
            remaining = snapshot["deadline"] - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                raise TimeoutError("规划时间预算已耗尽")
            options = material | ({"memory_context": memory} if memory.memories else {})
            from risk_platform.core.token_budget import request_tokens, project_read_messages, ContextBudgetExceeded
            from risk_platform.agents.investigation import initial_planning_request
            base, declarations = initial_planning_request(source, **options, skill_catalogue=snapshot["skills"])
            dialogue_limit = min(8000, 24000-request_tokens(base, declarations)-512)
            if dialogue_limit <= 0:
                raise ContextBudgetExceeded("当前正式规划材料没有可用读取空间")
            messages = planning_messages(events)
            projected = project_read_messages(messages, max_tokens=dialogue_limit, source_fields={
                "read_skill":("name", "resource", "digest"), "read_execution_result":("execution_id",)})
            if request_tokens(projected) > dialogue_limit:
                # 保留最后一批读取的完整请求与结果；摘要只覆盖更早的已完成交互。
                start = max((i for i, m in enumerate(projected) if m["role"] == "assistant"), default=0)
                if start <= 0:
                    raise ContextBudgetExceeded("当前完整规划交互超过预算")
                if sum(e["kind"] == "context" and e.get("summary") is not None for e in events) >= 2:
                    raise ContextBudgetExceeded("规划摘要次数已达上限，停止反复压缩")
                async with asyncio.timeout(remaining):
                    summary = await asyncio.to_thread(agent.compact_planning, projected[:start], timeout_seconds=remaining)
                projected = [dict(role="user", content="较早规划交互的有损摘要，不是案件证据：\n"+summary), *projected[start:]]
                if request_tokens(projected) >= request_tokens(messages) or request_tokens(projected) > dialogue_limit:
                    raise ContextBudgetExceeded("规划压缩没有释放足够空间")
                raw = [m for e in events if e["kind"] in {"model", "read"} for m in e["messages"]]
                last_id = projected[1]["tool_calls"][0]["id"]
                through = next(i for i, m in enumerate(raw) if any(c["id"] == last_id for c in m.get("tool_calls", [])))
            else:
                previous_context = next((e for e in reversed(events) if e["kind"] == "context"), {})
                through, summary = previous_context.get("through", 0), previous_context.get("summary")
            if projected != messages:
                omitted = [m["tool_call_id"] for m in projected if m.get("role") == "tool"
                           and json.loads(m["content"]).get("body_omitted")]
                async with sessions.begin() as session:
                    await authorize_run(session, runtime)
                    await invocation.guard.check_lock()
                    context_event = dict(kind="context", through=through, summary=summary, omitted_calls=omitted)
                    await append_planning_event(session, invocation.identity.run_id, events, context_event)
                    events.append(context_event)
                    await append_planning_event(session, invocation.identity.run_id, events, calling)
                events.append(calling)
                messages = planning_messages(events)
            remaining = snapshot["deadline"] - datetime.now(timezone.utc).timestamp()
            if remaining <= 0:
                raise TimeoutError("规划时间预算已耗尽")
            async with asyncio.timeout(min(remaining, trusted.timeout_seconds)):
                value = await asyncio.to_thread(agent.initial_plan, source.model_copy(deep=True), **options,
                    skill_catalogue=snapshot["skills"], skill_messages=messages)
            if datetime.now(timezone.utc).timestamp() >= snapshot["deadline"]:
                raise TimeoutError("规划时间预算已耗尽")
            from .contracts import InvestigationPlan
            if isinstance(value, InvestigationPlan):
                select_task_methods(value.tasks, planning_methods(events))
                if len(value.tasks) > material.get("remaining_task_budget", invocation.max_tasks):
                    raise ValueError("候选任务超过剩余预算")
                candidate = PlanningCandidate(plan=value)
                planning.validate(candidate, spec, source)
                event = dict(kind="plan", value=value.model_dump(mode="json"))
            else:
                calls = value.get("tool_calls", [])
                if not calls or len({c["id"] for c in calls}) != len(calls):
                    raise ValueError("规划工具调用为空或标识重复")
                previous_ids = {c["id"] for e in events if e["kind"] == "model"
                                for m in e["messages"] for c in m.get("tool_calls", [])}
                if previous_ids & {c["id"] for c in calls}:
                    raise ValueError("规划工具调用标识不能跨批次重复")
                candidate = None
                full, declarations = initial_planning_request(source, **options,
                    skill_catalogue=snapshot["skills"], skill_messages=messages)
                event = dict(kind="model", messages=[value],
                    read_budget=max(0, 24000-request_tokens([*full, value], declarations)-512))
        except Exception as error:
            await settle_planning_failure(sessions, invocation, runtime, error)
        async with sessions.begin() as session:
            await authorize_run(session, runtime)
            await invocation.guard.check_lock()
            await append_planning_event(session, invocation.identity.run_id, events, event)
            if candidate is not None:
                await planning.settle(session, attempt_id, status="COMMITTED", value=candidate)
        if candidate is None:
            return {"planning_next": "read_skill"}

    # 候选已可靠提交。首次生成和恢复都进入同一接受事务，不额外调度图节点。
    return await _accept_candidate(invocation, sessions=sessions, runtime=runtime, spec=spec,
                                   task_budget=material.get("remaining_task_budget", invocation.max_tasks))




async def read_skill_material(invocation, *, sessions, agent, runtime):
    """处理一批正式读取请求；只读取方法文件，不执行业务工具。"""
    from risk_platform.agents.skills import read_skill, SkillReadRequest, read_execution_result, ExecutionReadRequest
    from risk_platform.core.token_budget import ContextBudgetExceeded, estimate_tokens, request_tokens, paragraph_page
    async with sessions() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.identity.run_id)
        check_planning_retries(facts, invocation.policy)
        events = await read_planning_dialogue(session, invocation.identity.run_id)
    if not events:
        raise ledger.LedgerIntegrityError("读取缺少正式规划输入")
    if events[-1]["kind"] != "model":
        return {}
    snapshot = events[0]
    try:
        if datetime.now(timezone.utc).timestamp() >= snapshot["deadline"]:
            raise TimeoutError("规划时间预算已耗尽")
        calls = events[-1]["messages"][0]["tool_calls"]
        prior = [m for event in events if event["kind"] == "read" for m in event["messages"]]
        if len(prior) + len(calls) > snapshot["max_reads"]:
            raise ContextBudgetExceeded("方法读取次数已耗尽")
        failed_requests = set()
        for previous, event in zip(events, events[1:]):
            if event["kind"] == "read":
                responses = {m["tool_call_id"]: json.loads(m["content"]) for m in event["messages"]}
                for request in previous["messages"][0]["tool_calls"]:
                    if "error" in responses[request["id"]]:
                        failed_requests.add(json.dumps(request["function"], sort_keys=True))
        materials, replies, added = planning_methods(events), [], []
        page_budget = events[-1]["read_budget"] // max(1,len(calls))
        if page_budget < 256:
            raise ContextBudgetExceeded("当前规划请求没有足够的读取返回空间")
        for call in calls:
            if call.get("type") == "function" and call.get("function", {}).get("name") == read_execution_result.name:
                request = ExecutionReadRequest.model_validate_json(call["function"]["arguments"])
                async with sessions() as session:
                    await authorize_run(session, runtime)
                    await invocation.guard.check_lock()
                    row = await session.scalar(select(ResultRow).join(OperationRow,
                        ResultRow.operation_key == OperationRow.operation_key).where(
                        ResultRow.execution_id == request.execution_id,
                        OperationRow.investigation_id == runtime.investigation.investigation_id))
                    if row is None:
                        raise ValueError("执行结果不属于当前案件")
                    result = ledger.read_artifact(row).execution
                    access._check_registered_result(result, runtime)
                    from risk_platform.workflow.observation import result_page
                    budget = min(3000, page_budget)
                    result = result_page(result, start_item=request.start_item, max_tokens=budget)
                replies.append(dict(role="tool", tool_call_id=call["id"], content=json.dumps(result, ensure_ascii=False)))
                continue
            try:
                if call["type"] != "function" or call["function"]["name"] != read_skill.name:
                    raise ValueError("规划只能请求读取方法")
                request = SkillReadRequest.model_validate_json(call["function"]["arguments"])
            except (ValueError, KeyError):
                result = {"error": "INVALID_SKILL_REQUEST"}
            else:
                cached = next((m for m in [*(json.loads(p["content"]) for p in prior), *added]
                    if (m.get("name"), m.get("resource"), m.get("start_paragraph", 0)) ==
                       (request.name, request.resource, request.start_paragraph)), None)
                if cached:
                    active = [json.loads(m["content"]) for m in planning_messages(events) if m.get("role") == "tool"]
                    shown = any(m.get("name") == request.name and m.get("resource") == request.resource
                                and m.get("start_paragraph", 0) == request.start_paragraph and "content" in m for m in active)
                    if shown:
                        result = {"name": request.name, "resource": request.resource, "already_loaded": True}
                    else:
                        page = paragraph_page(cached["content"], start=0, max_tokens=page_budget-256)
                        next_page = page["next_paragraph"]
                        result = cached | dict(content=page["content"], next_paragraph=(
                            request.start_paragraph+next_page if next_page is not None else cached.get("next_paragraph")))
                elif request.resource != "SKILL.md" and not any(
                        m["name"] == request.name and m["resource"] == "SKILL.md" and m.get("next_paragraph") is None
                        for m in materials + added):
                    result = {"error": "READ_SKILL_INSTRUCTIONS_FIRST"}
                else:
                    remaining = snapshot["deadline"] - datetime.now(timezone.utc).timestamp()
                    async with asyncio.timeout(max(0, remaining)):
                        result = await read_skill.ainvoke(request.model_dump(), config={"configurable": {
                            "skill_directory": agent.skill_directory, "skill_catalogue": snapshot["skills"],
                            "body_budget":min(6000,page_budget-256)}})
                if "content" in result:
                    added.append(result)
            if "error" in result:
                key = json.dumps(call.get("function"), sort_keys=True)
                if key in failed_requests:
                    raise ValueError("重复无效读取请求")
                failed_requests.add(key)
            replies.append(dict(role="tool", tool_call_id=call["id"], content=json.dumps(result, ensure_ascii=False)))
        if request_tokens(replies) > events[-1]["read_budget"]:
            raise ContextBudgetExceeded("完整读取回复超过当前剩余预算")
        if estimate_tokens(json.dumps(materials + added, ensure_ascii=False)) > snapshot["method_tokens"]:
            raise ContextBudgetExceeded("方法材料超过累计 token 预算")
    except Exception as error:
        await settle_planning_failure(sessions, invocation, runtime, error)
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        await append_planning_event(session, invocation.identity.run_id, events, dict(kind="read", messages=replies))
    return {}


def planning_specification(execution, runtime, agent, material=None):
    """记录“按什么输入生成候选”，供规划日志登记和恢复时比较。

    这里不调用模型，也不生成任务。planning.register() 才负责把这份记录落库、
    比较同一运行是否仍使用相同的规划输入，避免把旧问题的候选用于新问题。
    """
    from risk_platform.agents.skills import PLANNING_SKILL_PROMPT, read_skill, read_execution_result
    from langchain_core.utils.function_calling import convert_to_openai_tool
    return PlanningInput(
        execution=execution.identity,  # 候选将归属的案件、轮次、运行及预留计划 ID。
        planning_model=agent.model,  # 只记录初始规划模型，不固定后续阶段的模型。
        planning_input_digest=input_digest(runtime.investigation.planning_input),
        # 保存提示词指纹用于比较；这不是提示词正文，也不保证模型输出完全一致。
        prompt_digest=hashlib.sha256((INITIAL_PLANNING_SYSTEM_PROMPT + PLANNING_SKILL_PROMPT +
            json.dumps([convert_to_openai_tool(t) for t in (read_skill, read_execution_result)], sort_keys=True) +
            (json.dumps(to_checkpoint_data(material), ensure_ascii=False, sort_keys=True)
             if material else "")).encode()).hexdigest(),
    )


async def load_accepted_plan(invocation, *, sessions, runtime):
    """读取正式计划；新调查的候选生成与接受由 prepare_plan 先完成。"""
    async with sessions() as session:
        await authorize_run(session, runtime)
        row = await session.get(AcceptedPlanRow, invocation.identity.plan_id)
        plan = ledger._read(row, AcceptedPlan).plan
    return to_checkpoint_data(dict(investigation_plan=plan, task_execution_results=[], observation=None,
                      round_complete=False, investigation_status="OPEN"))

# 模型这次会看到：
# 原始调查目标
# 历史发现和未解决问题
# 已执行任务及结果
# 当前能力
# 剩余任务预算
async def _accept_candidate(invocation, *, sessions, runtime, spec, task_budget):
    """正式接受已提交候选；不在接受事务中调用模型。"""
    async with sessions.begin() as session:
        trusted = await authorize_run(session, runtime)
        await invocation.guard.check_lock()
        facts = await planning.history(session, invocation.identity.run_id)
        fact = next((row for row in facts if row.status == "COMMITTED"), None)
        if fact is None:
            raise ledger.LedgerIntegrityError("正式接受必须关联可靠保存的候选计划")
        accepted = planning.candidate(fact, spec, runtime.investigation.planning_input)
        events = await read_planning_dialogue(session, invocation.identity.run_id)
        select_task_methods(accepted.plan.tasks, planning_methods(events))
        if len(accepted.plan.tasks) > task_budget:
            raise ledger.LedgerIntegrityError("下一轮计划超过累计任务预算")
        row = await ledger.accept_plan(session, accepted)
        session.add(PlanningAcceptanceRow(run_id=invocation.identity.run_id, attempt_id=fact.attempt_id,
            plan_id=accepted.plan_id, investigation_id=accepted.investigation_id,
            round_id=accepted.round_id, actor_id=trusted.actor_id))
        generation = await _bind_execution_run(session, invocation.identity, row, trusted, invocation.policy)
    # 数据库提交成功后才更新运行代次。
    invocation.guard.generation = generation
    return (await load_accepted_plan(invocation, sessions=sessions, runtime=runtime)) | {
        "planning_next": "task_execution"}

# | 参数 | 在这里的作用 |
# |---|---|
# | `state` | 流程当前的数据，包含希望使用的 `run_id`、`plan_id` |
# | `root` | Runner 最初建立的可信调用上下文，包含原始身份和已取得的运行锁 |
# | `sessions` | 创建数据库会话，用来核对正式轮次关系和运行记录 |
# 代码逻辑：
# 1. 从 State 读取目标 run_id。
# 2. 从 root 身份开始查找。
# 3. 检查权限和原运行锁。
# 4. 沿数据库正式后继关系逐轮前进。
# 5. 到达目标后，核对 plan_id。
# 6. 读取当前轮次及运行记录，构造当前上下文。
async def current_round_call(state, root, *, sessions):
    """Resolve checkpoint identity only through immutable successor links."""
    target = state.get("run_id", root.execution.identity.run_id)
    identity = root.execution.identity
    async with sessions() as session:
        await authorize_run(session, root.runtime)
        await root.execution.guard.check_lock()
# - 当前身份仍允许运行这个案件及起始轮次。
# - 本次执行持有的数据库运行锁仍然有效。
# 两者含义不同：有业务权限，不代表可以与另一个执行者同时推进；持有锁，也不代表用户有业务权限。
# 检查失败就抛异常，后面的节点不会继续执行。
        visited = set()
        # 其中 visited 保存已经走过的运行 ID，防止循环继承关系导致无限循环。
        while identity.run_id != target:
            if identity.run_id in visited:
                raise ledger.LedgerIntegrityError("Cyclic round succession")
            visited.add(identity.run_id)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            row = await session.get(RoundProgressionRow, identity.round_id)
            if row is None:
                raise ledger.LedgerIntegrityError("Checkpoint round has no accepted predecessor")
            link = ledger._read(row, RoundProgression)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            if (link.root_run_id != root.execution.identity.run_id or link.source_run_id != identity.run_id
                    or link.investigation_id != identity.investigation_id or not link.next_round_id):
                raise ledger.LedgerIntegrityError("Checkpoint successor is not part of this investigation run")
            identity = RunIdentity(run_id=link.next_run_id, investigation_id=link.investigation_id,
                round_id=link.next_round_id, plan_id=link.next_plan_id)
        if state.get("plan_id", identity.plan_id) != identity.plan_id:
            raise ledger.LedgerIntegrityError("Checkpoint plan differs from accepted successor")
        # 如果仍在第一轮，直接返回原始身份；否则返回 successor 链上找到的身份，供后续节点使用。
        if identity == root.execution.identity:
            return root
        record = ledger._read(await session.get(RoundRow, identity.round_id), RoundRecord)
        runtime = replace(root.runtime, round=record)
        await authorize_run(session, runtime)
        run = await session.get(InvestigationRunRow, identity.run_id)
        if run is not None and read_run_identity(run) != identity:
            raise ledger.LedgerIntegrityError("Successor execution identity differs from ledger")
        generation = run.current_generation if run is not None else 0
#     没有重新获取一把锁，而是继续使用原来的锁连接。
# 同时让新的 Guard 对应当前的 run_id 和代次：
    parent_guard = root.execution.guard
    guard = RunGuard(parent_guard.connection, parent_guard.backend_pid, identity.run_id, generation,
                     owner_id=parent_guard.owner_id)
    execution = RunExecutionContext(identity, root.execution.policy, guard,
        root.execution.max_rounds, root.execution.max_tasks, root.execution.max_duration_seconds)
    return InvestigationInvocation(runtime=runtime, execution=execution)

# 先确认当前轮次及其运行身份
#     ↓
# 再让 prepare_plan 使用正确身份生成并接受本轮计划


async def round_material(session, invocation, runtime):
# 从正式历史整理下一轮材料
# 以前查到了什么？做过哪些任务？还有多少任务额度？
# 它不调用模型，也不决定下一轮任务。
    observations, tasks = [], []
    links = list(await session.scalars(select(RoundProgressionRow).where(
        RoundProgressionRow.investigation_id == runtime.investigation.investigation_id,
        RoundProgressionRow.next_round_id.is_not(None)).order_by(RoundProgressionRow.created_at)))
    for row in links:
        link = ledger._read(row, RoundProgression)
        run = await session.get(InvestigationRunRow, link.source_run_id)
        identity = read_run_identity(run)
        past_runtime = replace(runtime, round=ledger._read(await session.get(RoundRow, identity.round_id), RoundRecord))
        await authorize_run(session, past_runtime)
        # Read operation history through the existing result authorization path.
        result_row = await session.get(ResultRow, link.observation_result_id)
        operation = business_projection(ledger.read_operation(await session.get(OperationRow, result_row.operation_key)))
        facts = await access.read_operation_history(session, operation, context=past_runtime)
        artifact = next((f.artifact for f in facts if f.artifact is not None and
            f.artifact.observation is not None and f.artifact.observation.observation_id == result_row.observation_id), None)
        if artifact is None:
            raise ledger.LedgerIntegrityError("Progression source observation is missing")
        observations.append(artifact.observation)
        plan = ledger._read(await session.get(AcceptedPlanRow, identity.plan_id), AcceptedPlan).plan
        #读取该轮 AcceptedPlan 和执行结果；而一轮里可能有多个任务，所以还需要内层循环：
        for task, summary in zip(plan.tasks, artifact.observation.execution_summaries, strict=True):
            executed = await session.scalar(select(ResultRow).where(ResultRow.execution_id == summary.execution_id))
            result_artifact = ledger.read_artifact(executed)
            result = result_artifact.execution
# ③ 整理任务摘要
# 主要保留：            
# 任务 ID、目标、预期信息
# 执行 ID、状态、结果类型
# 工具名
# 错误信息（有则提供）
# 实际请求和数据快照（有则提供）
            item = dict(task_id=task.task_id, objective=task.objective, expected_result=task.expected_result,
                        execution_id=result.execution_id, status=result.status.value, outcome=summary.outcome,
                        tool_name=result.tool_name or "knowledge_search")
#这些用于避免模型混淆：
# 零命中：查询成功，但没找到材料
# 失败：工具没完成工作
# 拒绝：不允许执行
            if result.error is not None:
                item["error"] = result.error.model_dump(mode="json")
            if result.payload is not None:
                payload = result.payload.model_dump(mode="json") if hasattr(result.payload, "model_dump") else result.payload
                # Keep actual request and versioned data identity, without copying all result bodies.
                if "request" in payload:
                    item["request"] = payload["request"]
                if "retrieved" in payload:
                    item["snapshot"] = payload["retrieved"]["snapshot"]
            operation = business_projection(ledger.read_operation(await session.get(OperationRow, executed.operation_key)))
            if operation.candidate_request is not None:
                item["request"] = to_checkpoint_data(operation.candidate_request)
            tasks.append(item)
    return dict(previous_observations=tuple(observations), previous_tasks=tuple(tasks),
                remaining_task_budget=invocation.max_tasks - len(tasks)) if observations else {}
# 算法上是：遍历历史轮次，再遍历每轮任务，组装模型输入摘要。 查询量也会随历史轮次和任务数增加，因为循环中有逐项数据库读取。

async def advance_round(state, call, root, *, sessions):
    """advance_round()：程序决定是否真正继续Atomically accept STOP or one successor; no model call in this transaction."""
    from risk_platform.workflow.persistence.recovery_models import InvestigationRunSelectionRow
    async with sessions.begin() as session:
        await authorize_run(session, call.runtime)
        await call.execution.guard.check(session)
        identity = call.execution.identity
        key = operation_identity_key(identity.investigation_id, identity.round_id, identity.plan_id, None, OperationType.OBSERVATION)
# InvestigationRunSelectionRow：记录“本轮选了哪个结果”
# ResultRow：保存“结果具体是什么”
# 选择表：
# run-1 + observation操作编号 → result-123
# 结果表：
# result-123 → 本轮发现、未解决问题、继续或停止建议
        selection = await session.get(InvestigationRunSelectionRow, (identity.run_id, key))
        if selection is None:
            raise ledger.LedgerIntegrityError("Cannot advance an uncommitted observation")
        result_row = await session.get(ResultRow, selection.result_id)
        observation = ledger.read_artifact(result_row).observation
        if observation != read_observation(to_checkpoint_data(state["observation"])):
            raise ledger.LedgerIntegrityError("Progression must consume the selected observation")

        # 检查当前轮次是否已经存在，如果存在，则检查是否属于当前调查链，以及是否与当前轮次的解释结果一致。
#         如果之前已保存决定，就核对根运行及来源结果，复用原决定。
# 这样即使推进记录提交后、检查点保存前中断，也不会恢复时创建另一轮。
        existing = await session.get(RoundProgressionRow, identity.round_id)
        if existing is not None:
            decision = ledger._read(existing, RoundProgression)
            if decision.root_run_id != root.execution.identity.run_id or decision.observation_result_id != result_row.result_id:
                raise ledger.LedgerIntegrityError("Round already belongs to another progression")
        else:
            material = await round_material(session, call.execution, call.runtime)
            spent = len(material.get("previous_tasks", ())) + len(state["task_execution_results"])
            number = call.runtime.round.round_number - root.runtime.round.round_number + 1
            reason = None
            if observation.status != "SUCCEEDED":
                reason = "本轮解释未能可靠完成；调查仍未解决"
            elif observation.action == "STOP":
                reason = observation.stop_reason
            elif not observation.unresolved_questions:
                reason = "没有剩余待调查问题；不代表已确认根因"
            elif number >= call.execution.max_rounds:
                reason = "调查轮次预算已耗尽；不代表根因已确认"
            elif spent >= call.execution.max_tasks:
                reason = "累计任务预算已耗尽；不代表根因已确认"
            origin = await session.get(InvestigationRunRow, root.execution.identity.run_id)
            if reason is None and (datetime.now(timezone.utc) - origin.created_at).total_seconds() >= call.execution.max_duration_seconds:
                reason = "调查时间预算已耗尽；不代表根因已确认"
            from risk_platform.execution.tools import TOOL_DEFINITIONS
            if reason is None and not any(b.name in TOOL_DEFINITIONS for b in call.runtime.tools):
                reason = "当前没有可用的已授权工具，无法继续调查"
            suffix = hashlib.sha256((root.execution.identity.run_id + identity.round_id).encode()).hexdigest()
            successor = {} if reason is not None else dict(next_round_id="round-" + suffix,
                next_run_id="run-" + suffix, next_plan_id="plan-" + suffix)
            # 这条推进记录必须属于本次调查链，必须从正在核对的这一轮出发，而且确实批准了下一轮。
            decision = RoundProgression(root_run_id=root.execution.identity.run_id, source_run_id=identity.run_id,
                investigation_id=identity.investigation_id, source_round_id=identity.round_id,
                observation_result_id=result_row.result_id, stop_reason=reason, **successor)
            if decision.next_round_id:
                next_round = RoundRecord(investigation_id=identity.investigation_id,
                    round_id=decision.next_round_id, round_number=call.runtime.round.round_number + 1)
                await ledger.register_round(session, next_round)
                # 新轮次也必须在当前授权范围内；失败时轮次及推进记录一并回滚。
                await authorize_run(session, replace(call.runtime, round=next_round))
            await ledger._insert(session, RoundProgressionRow, identity.round_id, decision,
                source_round_id=identity.round_id, investigation_id=identity.investigation_id,
                root_run_id=decision.root_run_id, source_run_id=identity.run_id,
                observation_result_id=result_row.result_id, next_round_id=decision.next_round_id)
    if decision.next_round_id:
        return dict(run_id=decision.next_run_id, plan_id=decision.next_plan_id,
            investigation_plan=None, task_execution_results=[], observation=None,
            round_complete=False, stop_reason=None, checkpoint_schema_version=3)
    return dict(round_complete=True, stop_reason=decision.stop_reason, investigation_status="OPEN", checkpoint_schema_version=3)
