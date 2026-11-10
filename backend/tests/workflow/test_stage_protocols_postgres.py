"""Stage configuration changes and immutable replay."""

import asyncio
import json

import pytest
from sqlalchemy import select

from risk_platform.workflow import phase_execution
from risk_platform.workflow.invocation import InvestigationInvocation
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.contracts import OperationType
from risk_platform.workflow.persistence.models import OperationRow, ResultRow, AttemptRow
from risk_platform.workflow.persistence.recovery_models import InvestigationRunRow
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy
from test_ledger_postgres import database, pytestmark
from test_recovery_postgres import Agent, ProcessLost, resources, seed, context, retrieval


@pytest.mark.parametrize("cut", ["before_observation", "after_query_commit", "during_query"])
def test_model_change_is_phase_local_and_never_overwrites_started_history(database, monkeypatch, cut):
    first = Agent()
    first.model = "model-A"
    second = Agent()
    second.model = "model-B"
    retrieval(monkeypatch, first)
    original = phase_execution._phase
    interrupted = []
    async def interrupt(sessions, execution, value, produce, **kwargs):
        if not interrupted and cut == "before_observation" and value.operation_type == OperationType.OBSERVATION:
            interrupted.append(True)
            raise ProcessLost("observation has not registered any operation")
        if not interrupted and value.operation_type == OperationType.QUERY_FORMULATION:
            if cut == "during_query":
                interrupted.append(True)
                async def lost():
                    raise ProcessLost("query STARTED but no reliable result")
                return await original(sessions, execution, value, lost, **kwargs)
            if cut == "after_query_commit":
                artifact = await original(sessions, execution, value, produce, **kwargs)
                interrupted.append(True)
                raise ProcessLost("query result committed, no node checkpoint")
        return await original(sessions, execution, value, produce, **kwargs)
    monkeypatch.setattr(phase_execution, "_phase", interrupt)
    async def scenario():
        async with resources(database, first, setup=True) as (sessions, _, runner):
            await seed(sessions, count=1)
            with pytest.raises(ProcessLost):
                await runner.ainvoke(plan_id="P", context=InvestigationInvocation(runtime=await context(sessions)))
            async with sessions() as session:
                before = {row.operation_key: (row.payload, row.input_digest) for row in await session.scalars(select(OperationRow))}
                old_results = {row.result_id: row.payload for row in await session.scalars(select(ResultRow))}
                run = (await session.scalars(select(InvestigationRunRow))).one()
                assert set(json.loads(run.payload)) == {"schema_version", "run_id", "investigation_id", "round_id", "plan_id"}
        retrieval(monkeypatch, second)
        async with resources(database, second) as (sessions, _, runner):
            invocation = InvestigationInvocation(runtime=await context(sessions))
            if cut == "during_query":
                with pytest.raises(ledger.LedgerConflictError):
                    await runner.ainvoke(plan_id="P", context=invocation, policy=RecoveryPolicy(retry_uncertain=True))
                assert second.events == []
            else:
                result = await runner.ainvoke(plan_id="P", context=invocation)
                assert result["round_complete"] is True
                assert "query:T0" not in second.events
                assert second.events[-1] == "observation"
            async with sessions() as session:
                operations = list(await session.scalars(select(OperationRow)))
                for row in operations:
                    if row.operation_key in before:
                        assert (row.payload, row.input_digest) == before[row.operation_key]
                for row in await session.scalars(select(ResultRow)):
                    if row.result_id in old_results:
                        assert row.payload == old_results[row.result_id]
                values = [ledger.read_operation(row) for row in operations]
                assert next(v for v in values if v.operation_type == OperationType.QUERY_FORMULATION).model_name == "model-A"
                if cut != "during_query":
                    assert next(v for v in values if v.operation_type == OperationType.OBSERVATION).model_name == "model-B"
                else:
                    assert len(list(await session.scalars(select(AttemptRow)))) == 1
    asyncio.run(scenario())
