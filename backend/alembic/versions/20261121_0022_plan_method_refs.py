"""为携带方法引用的候选与正式计划标记新契约，不改写历史记录。"""
from alembic import op
from pathlib import Path
import runpy

revision = "20261121_0022"
down_revision = "20261120_0021"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_ledger_plan", "investigation_plans", type_="check")
    op.create_check_constraint("ck_ledger_plan", "investigation_plans",
                               "schema_version IN (1,2) AND plan_version > 0")
    # 复用已有触发器改写工具；保留摘要、归属与终态不可变检查。
    migration = runpy.run_path(str(Path(__file__).with_name("20261114_0018_registered_tools.py")))
    migration["replace_trigger_body"]("planning_integrity", {
        "NEW.candidate::jsonb->>'schema_version' IS DISTINCT FROM '1'":
        "NEW.candidate::jsonb->>'schema_version' IS DISTINCT FROM '2'",
    })


def downgrade():
    raise RuntimeError("请先显式迁移携带方法引用的计划；不能静默降级不可变记录")
