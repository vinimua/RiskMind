"""AcceptedPlan 产生之前的稳定输入与候选产物，不扩展已封板的执行 Operation。"""

from .contracts import Digest, LedgerContract
from .recovery_contracts import FirstRoundSpec
from risk_platform.workflow.contracts import InvestigationPlan


class PlanningSpec(LedgerContract):
    execution: FirstRoundSpec
    planning_input_digest: Digest
    prompt_digest: Digest


class PlanningCandidate(LedgerContract):
    plan: InvestigationPlan
