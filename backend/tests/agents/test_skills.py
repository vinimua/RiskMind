import pytest
from risk_platform.agents.skills import SkillDirectory, SkillReadRequest, read_skill


def directory(tmp_path):
    folder = tmp_path / "drift"
    (folder / "references").mkdir(parents=True)
    (folder / "SKILL.md").write_text('---\nname: drift\ndescription: Investigate drift\n---\nRead references/psi.md when needed.', encoding="utf-8")
    (folder / "references/psi.md").write_text("PSI is a method, not case evidence.", encoding="utf-8")
    return SkillDirectory(tmp_path)


def test_catalogue_discloses_description_and_hashes_not_body(tmp_path):
    skills = directory(tmp_path)
    snapshot = skills.snapshot()
    assert snapshot[0]["name"] == "drift"
    assert "content" not in snapshot[0]
    assert set(snapshot[0]["files"]) == {"SKILL.md", "references/psi.md"}


def test_registered_reader_returns_only_requested_method(tmp_path):
    skills = directory(tmp_path)
    result = read_skill.invoke({"name": "drift"}, config={"configurable": {
        "skill_directory": skills, "skill_catalogue": skills.snapshot()}})
    assert "Read references" in result["content"]
    assert "PSI is a method" not in result["content"]
    assert result["kind"] == "METHOD_NOT_CASE_EVIDENCE"


@pytest.mark.parametrize("name,resource", [("unknown", "SKILL.md"), ("drift", "../secret"), ("drift", "/etc/passwd")])
def test_reader_rejects_unknown_name_and_paths(tmp_path, name, resource):
    skills = directory(tmp_path)
    assert skills.read(skills.snapshot(), SkillReadRequest(name=name, resource=resource))["error"] == "SKILL_RESOURCE_NOT_ALLOWED"


def test_changed_deployment_blocks_instead_of_supplying_new_content(tmp_path):
    skills = directory(tmp_path)
    snapshot = skills.snapshot()
    (tmp_path / "drift/references/psi.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="DEPLOYMENT_CHANGED"):
        skills.read(snapshot, SkillReadRequest(name="drift", resource="references/psi.md"))


def test_missing_resource_is_distinct_from_empty_content(tmp_path):
    skills = directory(tmp_path)
    snapshot = skills.snapshot()
    (tmp_path / "drift/references/psi.md").unlink()
    assert skills.read(snapshot, SkillReadRequest(name="drift", resource="references/psi.md"))["error"] == "SKILL_RESOURCE_MISSING"


def test_reference_pages_keep_frozen_digest_and_complete_paragraphs(tmp_path):
    skills = directory(tmp_path)
    (tmp_path/"drift/references/psi.md").write_text("first "*100+"\n\n"+"second "*100,encoding="utf-8")
    snapshot=skills.snapshot()
    first=skills.read(snapshot,SkillReadRequest(name="drift",resource="references/psi.md"),max_tokens=120)
    second=skills.read(snapshot,SkillReadRequest(name="drift",resource="references/psi.md",start_paragraph=first["next_paragraph"]),max_tokens=120)
    assert first["content"]=="first "*100 and second["content"]=="second "*100
    assert first["digest"]==second["digest"] and second["next_paragraph"] is None
