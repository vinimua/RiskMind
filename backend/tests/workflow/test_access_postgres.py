"""8B 真 PG：读/继续分权、当前范围、v1 兼容、v2 摘要与尝试级身份不可变。"""

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.workflow.access import (
    load_runtime_context, read_operation_history, register_authorized_operation, start_authorized_attempt,
)
from risk_platform.workflow.runtime import InvestigationAccessError, InvestigationAction
from risk_platform.workflow.persistence import repository as ledger
from risk_platform.workflow.persistence.business_inputs import BusinessOperationInput, business_projection
from risk_platform.workflow.persistence.contracts import CommittedArtifact, OperationType, RoundRecord, canonical_json, input_digest, operation_key
from risk_platform.workflow.persistence.models import AttemptRow, OperationRow
from test_ledger_contracts import accepted_plan, investigation, operation
from test_ledger_postgres import database, run, config, pytestmark  # shared UUID schema fixture, real PG only
from test_observation import execution


async def seed(sessions):
    async with sessions.begin() as session:
        await ledger.register_investigation(session, investigation())
        await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
        await ledger.accept_plan(session, accepted_plan())


async def runtime(session, actor="A", permission="both", **changes):
    options = dict(investigation_id="I", round_id="R", execution=KnowledgeExecutionContext(
        actor_id=actor, index_profile="parent-child-v1"), allow_unscoped=True,
        authorize_resource=lambda _, action, __: permission == "both" or permission == action,
        authorize_knowledge=lambda *_: True,
        action=InvestigationAction.READ if permission == "investigation:read" else InvestigationAction.CONTINUE)
    options.update(changes)
    execution = options.pop("execution")
    allow_unscoped = options.pop("allow_unscoped")
    authorize = options.pop("authorize_knowledge")
    return await load_runtime_context(session, actor_id=execution.actor_id,
        timeout_seconds=execution.timeout_seconds,
        tools=(KnowledgeSearchTool(execution, authorize, allow_unscoped),), **options)


@pytest.mark.parametrize("kind", list(OperationType))
def test_business_inputs_omit_actor_timeout_but_keep_stage_semantics(kind):
    original = operation(kind)
    changed = operation(kind, context=KnowledgeExecutionContext(actor_id="B", index_profile="baseline-v1", timeout_seconds=99))
    if kind == OperationType.KNOWLEDGE_EXECUTION:
        assert input_digest(business_projection(original)) != input_digest(business_projection(changed))
    else:
        assert input_digest(business_projection(original)) == input_digest(business_projection(changed))
    if kind == OperationType.QUERY_FORMULATION:
        changed = operation(kind, context=KnowledgeExecutionContext(actor_id="B", index_profile="baseline-v1", max_top_k=3))
        assert input_digest(business_projection(original)) != input_digest(business_projection(changed))
    for field in ("actor_id", "timeout_seconds", "context"):
        with pytest.raises(ValueError):
            BusinessOperationInput.model_validate({**business_projection(original).model_dump(), field: "forged"})


def test_actor_b_can_read_but_cannot_create_attempt(database):
    async def scenario(sessions):
        await seed(sessions)
        value = business_projection(operation())
        async with sessions.begin() as session:
            a = await runtime(session)
            key = await register_authorized_operation(session, value, context=a)
            await start_authorized_attempt(session, key, "A1", context=a)
            await ledger.commit_result(session, "A1", "result-1", CommittedArtifact(execution=execution(empty=True)))
        async with sessions.begin() as session:
            b = await runtime(session, "B", "investigation:read")
            history = await read_operation_history(session, value, context=b)
            assert history[0].artifact.execution.execution_id == "E0"
            assert history[0].actor_id == "A"
            with pytest.raises(InvestigationAccessError):
                await start_authorized_attempt(session, key, "B1", context=b, retry=True)
        async with sessions() as session:
            assert await session.get(AttemptRow, "B1") is None
    run(database, scenario)


def test_cross_actor_retry_has_own_attribution_and_original_creator(database):
    async def scenario(sessions):
        await seed(sessions)
        value = business_projection(operation())
        async with sessions.begin() as session:
            a = await runtime(session)
            key = await register_authorized_operation(session, value, context=a)
            await start_authorized_attempt(session, key, "A1", context=a)
            await ledger.settle_attempt(session, "A1", "UNCERTAIN", code="PROCESS_LOST")
        async with sessions.begin() as session:
            b = await runtime(session, "B", execution=KnowledgeExecutionContext(actor_id="B", index_profile="parent-child-v1", timeout_seconds=99))
            assert await register_authorized_operation(session, value, context=b) == key
            receipt = await start_authorized_attempt(session, key, "B1", context=b, retry=True)
            assert receipt.actor_id == "B"
            await ledger.commit_result(session, "B1", "result-2", CommittedArtifact(execution=execution(empty=True)))
        async with sessions() as session:
            b = await runtime(session, "B")
            history = await read_operation_history(session, value, context=b)
            assert [row.actor_id for row in history] == ["A", "B"]
            assert [row.status for row in history] == ["UNCERTAIN", "COMMITTED"]
            assert history[1].attribution.execution_context.timeout_seconds == 99
            row = await session.get(OperationRow, key)
            assert row.schema_version == 2 and row.created_by == "A"
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text("UPDATE investigation_attempts SET actor_id='C' WHERE attempt_id='B1'"))
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text("UPDATE investigation_operations SET created_by='C'"))
    run(database, scenario)


def test_continue_permission_does_not_grant_history_read(database):
    async def scenario(sessions):
        await seed(sessions)
        value = business_projection(operation())
        async with sessions.begin() as session:
            context = await runtime(session, permission="investigation:continue")
            key = await register_authorized_operation(session, value, context=context)
            receipt = await start_authorized_attempt(session, key, "A1", context=context)
            assert not hasattr(receipt, "artifact")
            with pytest.raises(InvestigationAccessError):
                await read_operation_history(session, value, context=context)
    run(database, scenario)


def test_revoked_permissions_and_wrong_case_never_return_payload(database):
    async def scenario(sessions):
        await seed(sessions)
        permission = {"allowed": True}
        policy = lambda *_: permission["allowed"]
        async with sessions.begin() as session:
            context = await runtime(session, authorize_resource=policy)
            key = await register_authorized_operation(session, business_projection(operation()), context=context)
        permission["allowed"] = False
        async with sessions.begin() as session:
            with pytest.raises(InvestigationAccessError):
                await read_operation_history(session, business_projection(operation()), context=context)
            with pytest.raises(InvestigationAccessError):
                await start_authorized_attempt(session, key, "A1", context=context)
        async with sessions.begin() as session:
            context = await runtime(session)
            with pytest.raises(InvestigationAccessError):
                await read_operation_history(session, business_projection(operation(round_id="other")), context=context)
    run(database, scenario)


def test_narrower_current_scope_cannot_read_broader_cached_result(database):
    async def scenario(sessions):
        await seed(sessions)
        value = business_projection(operation())
        async with sessions.begin() as session:
            a = await runtime(session)
            key = await register_authorized_operation(session, value, context=a)
            await start_authorized_attempt(session, key, "A1", context=a)
            await ledger.commit_result(session, "A1", "result-1", CommittedArtifact(execution=execution()))
        async with sessions.begin() as session:
            narrow = await runtime(session, "B", execution=KnowledgeExecutionContext(actor_id="B", index_profile="parent-child-v1",
                                                                                   scope={"document_id": "D0"}))
            with pytest.raises(InvestigationAccessError):
                await read_operation_history(session, value, context=narrow)
    run(database, scenario)


def test_new_attempt_without_actor_is_rejected_by_database(database):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            key = await register_authorized_operation(session, business_projection(operation()), context=await runtime(session))
        with pytest.raises(DBAPIError, match="requires authenticated actor"):
            async with sessions.begin() as session:
                session.add(AttemptRow(attempt_id="missing-actor", operation_key=key, attempt_number=1, status="STARTED"))
    run(database, scenario)


def create_legacy_before_0005(url):
    command.downgrade(config(), "20261103_0004")
    inv, plan, op = investigation(), accepted_plan(), operation()
    rnd = RoundRecord(investigation_id="I", round_id="R", round_number=1)
    artifact = CommittedArtifact(execution=execution(empty=True))
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for table, columns, value in (
                ("investigation_ledger", {"investigation_id": "I"}, inv),
                ("investigation_rounds", {"round_id": "R", "investigation_id": "I", "round_number": 1}, rnd),
                ("investigation_plans", {"plan_id": "P", "investigation_id": "I", "round_id": "R", "plan_version": 1}, plan),
            ):
                values = {**columns, "schema_version": 1, "payload": canonical_json(value), "input_digest": input_digest(value)}
                connection.execute(text(f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join(':'+k for k in values)})"), values)
            connection.execute(text("INSERT INTO investigation_plan_tasks(plan_id,task_id,ordinal,capability) VALUES ('P','T0',0,'KNOWLEDGE_RETRIEVAL')"))
            values = dict(operation_key=operation_key(op), investigation_id="I", round_id="R", plan_id="P", task_id="T0",
                operation_type="KNOWLEDGE_EXECUTION", schema_version=1, payload=canonical_json(op), input_digest=input_digest(op))
            connection.execute(text(f"INSERT INTO investigation_operations ({','.join(values)}) VALUES ({','.join(':'+k for k in values)})"), values)
            connection.execute(text("INSERT INTO investigation_attempts(attempt_id,operation_key,attempt_number,status) VALUES ('legacy-attempt',:key,1,'STARTED')"), {"key": operation_key(op)})
            connection.execute(text("""INSERT INTO investigation_results(result_id,attempt_id,operation_key,execution_id,schema_version,payload,input_digest)
                VALUES ('legacy-result','legacy-attempt',:key,'E0',1,:payload,:digest)"""),
                {"key": operation_key(op), "payload": canonical_json(artifact), "digest": input_digest(artifact)})
            connection.execute(text("UPDATE investigation_attempts SET status='COMMITTED',result_id='legacy-result',ended_at=CURRENT_TIMESTAMP WHERE attempt_id='legacy-attempt'"))
    finally:
        engine.dispose()
    command.upgrade(config(), "head")
    return op, artifact


def test_0004_legacy_read_and_cross_actor_continue_preserve_original_bytes(database):
    original, artifact = create_legacy_before_0005(database)
    async def scenario(sessions):
        value = business_projection(original)
        async with sessions.begin() as session:
            b = await runtime(session, "B", execution=KnowledgeExecutionContext(actor_id="B", index_profile="parent-child-v1", timeout_seconds=90))
            history = await read_operation_history(session, value, context=b)
            assert history[0].artifact == artifact
            assert history[0].actor_id is None and history[0].attribution is None
            key = await register_authorized_operation(session, value, context=b)
            await start_authorized_attempt(session, key, "B1", context=b, retry=True)
            row = await session.get(OperationRow, key)
            assert row.schema_version == 1 and row.created_by is None
            assert row.payload == canonical_json(original) and row.input_digest == input_digest(original)
        async with sessions() as session:
            legacy = await session.get(AttemptRow, "legacy-attempt")
            assert legacy.actor_id is None and legacy.runtime_payload is None
    run(database, scenario)


@pytest.mark.parametrize("scope", [None, "wrong-model", "M"])
def test_canonical_postgres_model_scope_binding(database, scope):
    async def scenario(sessions):
        record = investigation()
        record.planning_input.user_query.model_id = "M"
        async with sessions.begin() as session:
            await ledger.register_investigation(session, record)
            await ledger.register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
        async with sessions() as session:
            options = dict(execution=KnowledgeExecutionContext(actor_id="A", index_profile="parent-child-v1", scope={"model_id": scope}))
            if scope != "M":
                with pytest.raises(InvestigationAccessError):
                    await runtime(session, **options)
            else:
                context = await runtime(session, **options)
                assert context.investigation.planning_input.user_query.model_id == "M"
    run(database, scenario)


def test_retry_identity_cannot_be_reused_by_another_actor_and_downgrade_keeps_audit(database):
    async def scenario(sessions):
        await seed(sessions)
        async with sessions.begin() as session:
            a = await runtime(session)
            key = await register_authorized_operation(session, business_projection(operation()), context=a)
            await start_authorized_attempt(session, key, "A1", context=a)
        with pytest.raises(ledger.LedgerConflictError):
            async with sessions.begin() as session:
                await start_authorized_attempt(session, key, "A1", context=await runtime(session, "B"))
    run(database, scenario)
    with pytest.raises(DBAPIError, match="cannot discard runtime identity audit history"):
        command.downgrade(config(), "20261103_0004")


def test_observation_read_rechecks_underlying_results_current_profile(database):
    from risk_platform.workflow.observation import inconclusive_observation
    async def scenario(sessions):
        await seed(sessions)
        result = execution(empty=True)
        value = business_projection(operation(OperationType.OBSERVATION))
        async with sessions.begin() as session:
            a = await runtime(session)
            key = await register_authorized_operation(session, business_projection(operation()), context=a)
            await start_authorized_attempt(session, key, "A1", context=a)
            await ledger.commit_result(session, "A1", "result-1", CommittedArtifact(execution=result))
            obs_key = await register_authorized_operation(session, value, context=a)
            await start_authorized_attempt(session, obs_key, "O1", context=a)
            await ledger.commit_result(session, "O1", "observation-1", CommittedArtifact(
                observation=inconclusive_observation([result], "NO_HITS", "无知识")))
        async with sessions() as session:
            a = await runtime(session)
            assert (await read_operation_history(session, value, context=a))[0].artifact.observation.status == "INCONCLUSIVE"
            b = await runtime(session, "B", execution=KnowledgeExecutionContext(actor_id="B", index_profile="baseline-v1"))
            with pytest.raises(InvestigationAccessError):
                await read_operation_history(session, value, context=b)
    run(database, scenario)
