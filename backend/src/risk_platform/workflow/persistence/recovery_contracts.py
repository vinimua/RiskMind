"""8C 从不可变 Accepted Plan 开始恢复首轮，不恢复尚未接受的初始规划。"""

from typing import Literal

from pydantic import Field

from risk_platform.knowledge.contracts import KnowledgeFilters
from risk_platform.knowledge.indexing.profiles import IndexProfile
from .contracts import CommittedArtifact, Digest, Identity, LedgerContract


class PhaseArtifact(CommittedArtifact):
    # v2 允许 QUERY_FORMULATION 保存其已知失败执行事实，避免恢复时重新生成失败身份。
    schema_version: Literal[2] = 2


class FirstRoundSpec(LedgerContract):
    run_id: Identity
    investigation_id: Identity
    round_id: Identity
    plan_id: Identity
    index_profile: IndexProfile
    scope: KnowledgeFilters
    max_top_k: int = Field(gt=0, strict=True)
    query_model: Identity
    query_prompt_digest: Digest
    observation_model: Identity
    observation_prompt_digest: Digest


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
