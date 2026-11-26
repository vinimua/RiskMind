"""AcceptedPlan 产生之前的稳定输入与候选产物，不扩展已封板的执行 Operation。"""

from typing import Literal
from .contracts import Digest, Identity, LedgerContract
from .recovery_contracts import RunIdentity
from risk_platform.workflow.contracts import InvestigationPlan


class PlanningCandidate(LedgerContract):
    schema_version: Literal[2] = 2
    plan: InvestigationPlan


class PlanningInput(LedgerContract):
    """初始规划阶段输入；仅固定规划本身，不预先固定后续阶段配置。"""

    schema_version: Literal[2] = 2
    execution: RunIdentity
    planning_model: Identity
    planning_input_digest: Digest
    prompt_digest: Digest
