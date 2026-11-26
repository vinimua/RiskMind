"""任务只能引用本轮实际读取的方法，模型输入按正式任务投影。"""
import pytest
from risk_platform.workflow.contracts import InvestigationPlan, InvestigationTask, SkillMaterialRef
from risk_platform.workflow.planning import select_task_methods


def plan(*refs):
    return InvestigationPlan(goal="调查", tasks=[InvestigationTask(objective="检查缺失率",
        expected_result="统计结果", capability="COMPUTATION", skill_refs=refs)])


def test_task_methods_select_only_referenced_files_and_reject_unread_files():
    materials = [dict(name="quality", resource="SKILL.md", content="方法"),
                 dict(name="unused", resource="SKILL.md", content="无关方法")]
    ref = SkillMaterialRef(skill_name="quality", resource="SKILL.md")
    assert select_task_methods(plan(ref).tasks, materials) == (materials[0],)
    assert select_task_methods(plan().tasks, materials) == ()
    with pytest.raises(ValueError, match="未读取"):
        select_task_methods(plan(ref).tasks, [])
    with pytest.raises(ValueError, match="重复"):
        select_task_methods(plan(ref, ref).tasks, materials)


def test_shared_methods_are_supplied_once_to_observation():
    ref = SkillMaterialRef(skill_name="quality", resource="SKILL.md")
    tasks = [plan(ref).tasks[0], plan(ref).tasks[0]]
    material = dict(name="quality", resource="SKILL.md", content="正文")
    assert select_task_methods(tasks, [material]) == (material,)
