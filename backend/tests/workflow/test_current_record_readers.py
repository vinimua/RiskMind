"""Current run/planning records validate digests; unsupported formats fail closed."""

from types import SimpleNamespace

import pytest

from risk_platform.workflow.persistence.contracts import canonical_json, input_digest
from risk_platform.workflow.persistence.recovery_contracts import RunIdentity, read_run_identity
from risk_platform.workflow.persistence.planning_contracts import PlanningInput
from risk_platform.workflow.persistence.planning_repository import read_spec
from risk_platform.workflow.persistence.repository import LedgerIntegrityError


def records():
    identity = RunIdentity(run_id="RUN", investigation_id="I", round_id="R", plan_id="P")
    return identity, PlanningInput(execution=identity, planning_model="model",
        planning_input_digest="0" * 64, prompt_digest="1" * 64)


@pytest.mark.parametrize("index,reader", [(0, read_run_identity), (1, read_spec)])
def test_current_record_can_be_read_and_input_digest_is_checked(index, reader):
    value = records()[index]
    row = SimpleNamespace(schema_version=2, payload=canonical_json(value), input_digest=input_digest(value))
    assert reader(row) == value
    row.input_digest = "f" * 64
    with pytest.raises(LedgerIntegrityError):
        reader(row)


@pytest.mark.parametrize("version", [1, 0, 3])
@pytest.mark.parametrize("reader", [read_run_identity, read_spec])
def test_unsupported_record_version_is_rejected_before_payload_parsing(version, reader):
    row = SimpleNamespace(schema_version=version, payload="not JSON", input_digest="")
    with pytest.raises(LedgerIntegrityError, match="仅支持 v2"):
        reader(row)
