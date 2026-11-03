import pytest

from risk_platform.workflow.checkpoints import JsonCheckpointSerializer
from risk_platform.workflow.persistence.recovery_contracts import RecoveryPolicy


def test_checkpoint_json_only_and_error_body_not_persisted():
    serde = JsonCheckpointSerializer()
    assert serde.loads_typed(serde.dumps_typed({"中文": [1, True, None]})) == {"中文": [1, True, None]}
    encoded = serde.dumps_typed(RuntimeError("sk-secret-provider-body"))
    assert b"secret" not in encoded[1]
    assert serde.loads_typed(encoded) == {"workflow_error_type": "RuntimeError"}
    for kind in ("pickle", "msgpack", "constructor"):
        with pytest.raises(ValueError):
            serde.loads_typed((kind, b"{}"))
    with pytest.raises(ValueError):
        serde.loads(b"NaN")
    with pytest.raises(TypeError):
        serde.dumps(object())


def test_recovery_policy_is_explicit_strict_and_not_an_automatic_retry_default():
    assert not RecoveryPolicy().retry_uncertain and not RecoveryPolicy().retry_failed_attempts
    with pytest.raises(ValueError):
        RecoveryPolicy(retry_uncertain="yes")
    with pytest.raises(ValueError):
        RecoveryPolicy(automatic_retry=True)
