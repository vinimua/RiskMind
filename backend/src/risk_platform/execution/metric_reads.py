"""指标定义的请求阶段记录；模型等待在事务外，只有正式任务可追加。"""
import hashlib
import json
from sqlalchemy import ForeignKey, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column
from risk_platform.db.database import Base
from risk_platform.execution.metrics import DefinitionRequest, DefinitionPage, binding
from risk_platform.workflow.access import authorize_run


class QueryReadRow(Base):
    __tablename__ = "investigation_query_reads"
    run_id: Mapped[str] = mapped_column(String(128), ForeignKey("investigation_runs.run_id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    digest: Mapped[str] = mapped_column(String(64))


async def read_events(session, run_id, task_id):
    rows = list(await session.scalars(select(QueryReadRow).where(
        QueryReadRow.run_id == run_id, QueryReadRow.task_id == task_id).order_by(QueryReadRow.sequence)))
    for index, row in enumerate(rows):
        if row.sequence != index or hashlib.sha256(row.payload.encode()).hexdigest() != row.digest:
            raise ValueError("指标定义读取记录损坏")
    return [json.loads(row.payload) for row in rows]


async def read_definition(call, *, sessions, invocation, runtime, task_id):
    request = DefinitionRequest.model_validate(call["args"])
    if request.model_id != binding(runtime).model_id:
        raise PermissionError("指标目录不属于当前模型")
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check(session)
        events = await read_events(session, invocation.identity.run_id, task_id)
    if len(events) >= 4:
        raise ValueError("指标目录读取次数已耗尽")
    # 重复范围可复用已提交正文；服务端数据读取保持只读。
    prior = next((e for e in events if e["request"] == request.model_dump(mode="json")), None)
    page = (DefinitionPage.model_validate(prior["result"]) if prior else
        await binding(runtime).client.call("list_metric_definitions", request))
    event = dict(request=request.model_dump(mode="json"), result=page.model_dump(mode="json"), call=call)
    async with sessions.begin() as session:
        await authorize_run(session, runtime)
        await invocation.guard.check(session)
        current = await read_events(session, invocation.identity.run_id, task_id)
        if current != events:
            raise ValueError("指标读取记录发生并发变化")
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True)
        session.add(QueryReadRow(run_id=invocation.identity.run_id, task_id=task_id,
            sequence=len(events), payload=payload, digest=hashlib.sha256(payload.encode()).hexdigest()))
    return event


def messages(events):
    return [message for event in events for message in (
        dict(role="assistant", content="", tool_calls=[dict(id=event["call"]["id"], type="function",
            function=dict(name="list_metric_definitions", arguments=json.dumps(event["request"]))) ]),
        dict(role="tool", tool_call_id=event["call"]["id"], content=json.dumps(event["result"], ensure_ascii=False)),
    )]
