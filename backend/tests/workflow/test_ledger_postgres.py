"""真实 PostgreSQL：迁移、复合 FK、不可变历史与并发；只操作 UUID 临时 schema。"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, make_url, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.workflow.observation import KnowledgeObservationDraft, complete_observation, inconclusive_observation
from risk_platform.workflow.persistence.contracts import (
    AttemptStatus, CommittedArtifact, OperationType, RoundRecord, canonical_json, input_digest, operation_key,
)
from risk_platform.workflow.persistence.models import AcceptedPlanRow, AttemptRow, OperationRow, ResultRow
from risk_platform.workflow.persistence.repository import (
    LedgerConflictError, LedgerIntegrityError, accept_plan, commit_result, inspect_operation,
    register_investigation, register_operation, register_round, settle_attempt, start_attempt,
)
from test_ledger_contracts import accepted_plan, investigation, operation
from test_observation import draft, execution


pytestmark = pytest.mark.skipif(os.environ.get("RUN_POSTGRES_INTEGRATION") != "1",
                                reason="set RUN_POSTGRES_INTEGRATION=1 and RISK_DATABASE_URL for real PostgreSQL")


def config():
    backend = Path(__file__).resolve().parents[2]
    result = Config(str(backend / "alembic.ini"))
    result.set_main_option("script_location", str(backend / "alembic"))
    result.set_main_option("path_separator", "os")
    return result


@pytest.fixture
def database(monkeypatch):
    schema = "test_ledger_" + uuid4().hex
    url = make_url(os.environ["RISK_DATABASE_URL"])
    admin = create_engine(url)
    isolated = url.update_query_dict({"options": f"-csearch_path={schema}"})
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        monkeypatch.setenv("RISK_DATABASE_URL", isolated.render_as_string(hide_password=False))
        command.upgrade(config(), "20261025_0003")
        engine = create_engine(isolated)
        try:
            with engine.begin() as connection:
                connection.execute(text("""INSERT INTO knowledge_chunks
                    (chunk_id,index_profile,document_id,document_version,document_type,title,section_path,content,source_uri,ingestion_status)
                    VALUES ('legacy','baseline-v1','D','v1','INCIDENT_REPORT','Legacy','','unchanged','knowledge://legacy','PENDING')"""))
            command.upgrade(config(), "head")
            with engine.connect() as connection:
                assert connection.execute(text("SELECT chunk_id,content,ingestion_status FROM knowledge_chunks")).one() == (
                    "legacy", "unchanged", "PENDING")
        finally:
            engine.dispose()
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        yield isolated
    finally:
        # schema is constructed only from a fixed prefix and UUID, never user input.
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


async def seed(sessions, value=None, *, with_operation=True):
    async with sessions.begin() as session:
        await register_investigation(session, investigation())
        await register_round(session, RoundRecord(investigation_id="I", round_id="R", round_number=1))
        await accept_plan(session, accepted_plan())
        if with_operation:
            return (await register_operation(session, value or operation())).operation_key


def run(database, body):
    async def scenario():
        engine = create_async_engine(database)
        try:
            await body(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,empty", [("SUCCEEDED", False), ("SUCCEEDED", True), ("FAILED", False), ("REJECTED", False)])
def test_committed_is_storage_fact_and_reuse_preserves_identity_snapshot(database, status, empty):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            assert await inspect_operation(session, operation()) == ()
            await start_attempt(session, key, "A1")
        artifact = CommittedArtifact(execution=execution(status=status, empty=empty))
        async with sessions.begin() as session:
            assert (await commit_result(session, "A1", "result-1", artifact)).result_id == "result-1"
        async with sessions.begin() as session:
            assert (await commit_result(session, "A1", "result-1", artifact)).execution_id == "E0"
            history = await inspect_operation(session, operation())
            assert history[0][0].status == "COMMITTED"
            assert history[0][1] == artifact
            assert history[0][1].execution.status == status
            assert await inspect_operation(session, operation(plan_id="other")) is None
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await commit_result(session, "A1", "replacement", artifact)
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                changed = artifact.execution.model_copy(update={"execution_id": "changed"})
                await commit_result(session, "A1", "result-1", CommittedArtifact(execution=changed))
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await register_operation(session, operation(context={"actor_id": "other", "index_profile": "parent-child-v1"}))
    run(database, scenario)


@pytest.mark.parametrize("terminal", [AttemptStatus.FAILED, AttemptStatus.UNCERTAIN])
def test_attempt_terminal_history_and_explicit_retry(database, terminal):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await start_attempt(session, key, "A2", retry=True)
        async with sessions.begin() as session:
            await settle_attempt(session, "A1", terminal, code="CALL_OUTCOME")
            await settle_attempt(session, "A1", terminal, code="CALL_OUTCOME")
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await settle_attempt(session, "A1", AttemptStatus.UNCERTAIN if terminal == AttemptStatus.FAILED else AttemptStatus.FAILED, code="OTHER")
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await start_attempt(session, key, "A2")
        async with sessions.begin() as session:
            await start_attempt(session, key, "A2", retry=True)
            await commit_result(session, "A2", "result-2", CommittedArtifact(execution=execution()))
        async with sessions.begin() as session:
            history = await inspect_operation(session, operation())
            assert [a.status for a, _ in history] == [terminal, "COMMITTED"]
            assert [a.attempt_number for a, _ in history] == [1, 2]
            assert history[0][1] is None
    run(database, scenario)


def test_concurrent_operation_registration_and_attempt_claim(database):
    async def scenario(sessions):
        await seed(sessions, with_operation=False)
        async def register():
            async with sessions.begin() as session:
                return (await register_operation(session, operation())).operation_key
        keys = await asyncio.gather(register(), register())
        assert keys[0] == keys[1]
        key = keys[0]
        async def claim(attempt_id):
            async with sessions.begin() as session:
                return (await start_attempt(session, key, attempt_id)).attempt_id
        results = await asyncio.gather(claim("A1"), claim("A2"), return_exceptions=True)
        assert sum(isinstance(r, LedgerConflictError) for r in results) == 1
        winner = next(r for r in results if isinstance(r, str))
        artifact = CommittedArtifact(execution=execution())
        async def commit():
            async with sessions.begin() as session:
                return (await commit_result(session, winner, "result-1", artifact)).result_id
        assert await asyncio.gather(commit(), commit()) == ["result-1", "result-1"]
        async with sessions() as session:
            assert len(list(await session.scalars(select(OperationRow)))) == 1
            assert len(list(await session.scalars(select(ResultRow)))) == 1
    run(database, scenario)


def test_plan_immutable_and_invalid_relationships_fail_before_result(database):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            await accept_plan(session, accepted_plan())
        changed = accepted_plan()
        changed.plan.goal = "changed"
        with pytest.raises(LedgerConflictError):
            async with sessions.begin() as session:
                await accept_plan(session, changed)
        for value in (operation(round_id="other"), operation(task_id="other")):
            with pytest.raises(LedgerIntegrityError):
                async with sessions.begin() as session:
                    await register_operation(session, value)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
        with pytest.raises(LedgerIntegrityError):
            async with sessions.begin() as session:
                await commit_result(session, "A1", "result-1", CommittedArtifact(execution=execution(index=1)))
        async with sessions() as session:
            assert (await session.get(AttemptRow, "A1")).status == "STARTED"
            assert list(await session.scalars(select(ResultRow))) == []
    run(database, scenario)


@pytest.mark.parametrize("sql", [
    "UPDATE investigation_results SET payload = '{}'",
    "UPDATE investigation_attempts SET status='FAILED',result_id=NULL,resolution_code='overwrite'",
    "DELETE FROM investigation_results",
    "DELETE FROM investigation_attempts",
    "UPDATE investigation_plans SET plan_version=2",
    "INSERT INTO investigation_plan_tasks(plan_id,task_id,ordinal,capability) VALUES ('missing','T1',0,'KNOWLEDGE_RETRIEVAL')",
    "INSERT INTO investigation_plan_tasks(plan_id,task_id,ordinal,capability) VALUES ('P','T0',0,'KNOWLEDGE_RETRIEVAL')",
    "UPDATE investigation_rounds SET investigation_id='missing'",
])
def test_database_rejects_history_mutation_fk_and_unique_violations(database, sql):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
            await commit_result(session, "A1", "result-1", CommittedArtifact(execution=execution()))
        with pytest.raises(DBAPIError):
            async with sessions.begin() as session:
                await session.execute(text(sql))
    run(database, scenario)


def test_deferred_fk_prevents_committed_without_result_and_transaction_rollback(database):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                await session.execute(text("""UPDATE investigation_attempts SET status='COMMITTED',
                    result_id='missing', ended_at=CURRENT_TIMESTAMP WHERE attempt_id='A1'"""))
        with pytest.raises(RuntimeError):
            async with sessions.begin() as session:
                await commit_result(session, "A1", "result-1", CommittedArtifact(execution=execution()))
                raise RuntimeError("simulated caller rollback")
        async with sessions() as session:
            assert (await session.get(AttemptRow, "A1")).status == "STARTED"
            assert await session.get(ResultRow, "result-1") is None
    run(database, scenario)


@pytest.mark.parametrize("inconclusive", [False, True])
def test_observation_identity_references_and_payload_are_persisted(database, inconclusive):
    async def scenario(sessions):
        key = await seed(sessions)
        result = execution()
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
            await commit_result(session, "A1", "result-1", CommittedArtifact(execution=result))
        value = operation(OperationType.OBSERVATION)
        async with sessions.begin() as session:
            row = await register_operation(session, value)
            await start_attempt(session, row.operation_key, "observation-attempt")
        observation = (inconclusive_observation([result], "MODEL_UNAVAILABLE", "解释不可用") if inconclusive
                       else complete_observation([result], KnowledgeObservationDraft.model_validate(draft([result]))))
        async with sessions.begin() as session:
            await commit_result(session, "observation-attempt", "observation-result", CommittedArtifact(observation=observation))
        async with sessions() as session:
            history = await inspect_operation(session, value)
            assert history[0][1].observation == observation
    run(database, scenario)


def test_query_artifact_and_populated_downgrade_guard(database):
    async def scenario(sessions):
        value = operation(OperationType.QUERY_FORMULATION)
        key = await seed(sessions, value)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
            await commit_result(session, "A1", "query-result", CommittedArtifact(query_request=operation().candidate_request))
        async with sessions() as session:
            assert (await inspect_operation(session, value))[0][1].query_request == operation().candidate_request
    run(database, scenario)
    with pytest.raises(DBAPIError, match="cannot discard execution ledger history"):
        command.downgrade(config(), "20261025_0003")


@pytest.mark.parametrize("changes", [{"round_id": "wrong-round"}, {"task_id": "wrong-task"}])
def test_composite_fk_rejects_cross_round_or_unknown_task_even_with_valid_payload(database, changes):
    async def scenario(sessions):
        await seed(sessions)
        value = operation(OperationType.QUERY_FORMULATION, **changes)
        with pytest.raises(IntegrityError):
            async with sessions.begin() as session:
                session.add(OperationRow(operation_key=operation_key(value), investigation_id=value.investigation_id,
                    round_id=value.round_id, plan_id=value.plan_id, task_id=value.task_id,
                    operation_type=value.operation_type.value, payload=canonical_json(value), input_digest=input_digest(value), schema_version=1))
    run(database, scenario)


def test_accepted_plan_cannot_commit_without_normalized_tasks(database):
    async def scenario(sessions):
        await seed(sessions)
        value = accepted_plan().model_copy(update={"plan_id": "P2", "plan_version": 2})
        with pytest.raises(DBAPIError, match="tasks are incomplete"):
            async with sessions.begin() as session:
                session.add(AcceptedPlanRow(plan_id=value.plan_id, investigation_id="I", round_id="R", plan_version=2,
                    schema_version=1, payload=canonical_json(value), input_digest=input_digest(value)))
    run(database, scenario)


def test_empty_ledger_downgrade_upgrade_and_model_schema_agree(database):
    command.downgrade(config(), "20261025_0003")
    command.upgrade(config(), "head")
    engine = create_engine(database)
    try:
        inspector = inspect(engine)
        from risk_platform.db.database import Base
        for name, table in Base.metadata.tables.items():
            if not name.startswith("investigation_"):
                continue
            actual = {column["name"]: column for column in inspector.get_columns(name)}
            assert set(actual) == set(table.columns.keys())
            assert inspector.get_pk_constraint(name)["constrained_columns"] == [column.name for column in table.primary_key]
            for column in table.columns:
                assert actual[column.name]["nullable"] == column.nullable
            actual_fk = {tuple(fk["constrained_columns"]): (fk["referred_table"], tuple(fk["referred_columns"]))
                         for fk in inspector.get_foreign_keys(name)}
            for fk in table.foreign_key_constraints:
                assert actual_fk[tuple(column.name for column in fk.columns)] == (
                    next(iter(fk.elements)).column.table.name, tuple(element.column.name for element in fk.elements))
    finally:
        engine.dispose()


def test_reused_session_refreshes_attempt_after_another_writer_commits(database):
    async def scenario(sessions):
        key = await seed(sessions)
        async with sessions.begin() as session:
            await start_attempt(session, key, "A1")
        async with sessions() as reader:
            before = await inspect_operation(reader, operation())
            assert before[0][0].status == "STARTED"
            await reader.commit()  # expire_on_commit=False deliberately retains ORM identity.
            async with sessions.begin() as writer:
                await commit_result(writer, "A1", "result-1", CommittedArtifact(execution=execution()))
            after = await inspect_operation(reader, operation())
            assert after[0][0].status == "COMMITTED"
            assert after[0][1].execution.execution_id == "E0"
    run(database, scenario)
