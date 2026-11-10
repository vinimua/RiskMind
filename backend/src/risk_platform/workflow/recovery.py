"""旧导入路径的兼容导出；执行实现已迁至 runner/planning/phase_execution/run_guard。

新代码使用 runner.InvestigationRunner。这里不定义节点、事务或另一条执行入口。
"""

from risk_platform.workflow.runner import InvestigationRunner
from risk_platform.workflow.planning import PlanningPreparationError
from risk_platform.workflow.run_guard import RecoveryBlockedError, RecoveryFencedError

# 同一个类的旧名称，兼容已有调用方；不是第二套执行实现。
FirstRoundRecovery = InvestigationRunner

__all__ = ["InvestigationRunner", "FirstRoundRecovery", "PlanningPreparationError",
           "RecoveryBlockedError", "RecoveryFencedError"]
