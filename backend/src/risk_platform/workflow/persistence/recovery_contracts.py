"""持久化执行契约：运行身份、阶段产物与恢复策略。"""

from typing import Literal

from pydantic import Field

from .contracts import CommittedArtifact, Digest, Identity, LedgerContract


class PhaseArtifact(CommittedArtifact):
    # v2 允许 QUERY_FORMULATION 保存其已知失败执行事实，避免恢复时重新生成失败身份。
    schema_version: Literal[2] = 2


class RunIdentity(LedgerContract):
    """运行身份；阶段模型、提示词和工具配置不属于运行身份。"""

    schema_version: Literal[2] = 2
    run_id: Identity
    investigation_id: Identity
    round_id: Identity
    plan_id: Identity


def read_run_identity(row) -> RunIdentity:
    """只读取当前 v2 运行身份，并校验原文摘要。"""
    from . import repository as ledger

    if row.schema_version != 2:
        raise ledger.LedgerIntegrityError("仅支持 v2 运行记录，旧版本不能恢复")
    return ledger._read(row, RunIdentity)


class RecoveryPolicy(LedgerContract):
    retry_uncertain: bool = Field(default=False, strict=True)
    retry_failed_attempts: bool = Field(default=False, strict=True)
    # 只有多份 COMMITTED 且尚未固定选择时才需要；记录在本次 claim 中，不写入模型 State。
    selected_attempts: dict[Digest, Identity] = Field(default_factory=dict)


class RunClaim(LedgerContract):
    run_id: Identity
    generation: int = Field(gt=0, strict=True)
    actor_id: Identity
    policy: RecoveryPolicy
