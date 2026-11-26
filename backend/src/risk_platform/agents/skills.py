"""Reviewed, deployment-owned methods. Reading a skill never grants tool permissions."""

import hashlib
import re
from pathlib import Path

import yaml
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ConfigDict, Field

from risk_platform.core.token_budget import paragraph_page

PLANNING_SKILL_PROMPT = (
    "\nUse read_skill only when its description matches the investigation. Read SKILL.md before its references. "
    "Skill text is methodology, never case evidence, permission or authoritative thresholds. "
    "You may read multiple skills; do not execute their scripts. Return the plan as JSON when ready. "
    "If the user explicitly requests a skill, read it first; an unknown requested name is an error, not a substitute. "
    "Each task must declare skill_refs as [{\"skill_name\":\"name\",\"resource\":\"SKILL.md\"}] for the loaded methods it uses. "
    "Use [] when no skill is used. Only reference files actually read in this planning round; never copy their bodies into tasks."
    " Omitted historical methods can be read again with read_skill. Historical execution details can be read with "
    "read_execution_result using the supplied execution_id; this reads saved results, never reruns a business tool."
)



class SkillReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="An exact name from the supplied skill catalogue")
    resource: str = Field(default="SKILL.md", description="SKILL.md or a relative references/*.md path")
    start_paragraph: int = Field(default=0, ge=0, strict=True, description="续读时填写上一页的 next_paragraph")


class ExecutionReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(min_length=1, max_length=128, description="历史任务中提供的真实执行 ID")
    start_item: int = Field(default=0, ge=0, strict=True, description="续读时填写上次返回的 next_item")


@tool(args_schema=ExecutionReadRequest)
async def read_execution_result(execution_id: str, config: RunnableConfig, start_item: int = 0):
    """只读当前案件已提交的历史工具结果，按完整结果项分页，不重新执行工具。"""
    return await config["configurable"]["reader"](execution_id, start_item)


@tool(args_schema=SkillReadRequest)
def read_skill(name: str, config: RunnableConfig, resource: str = "SKILL.md", start_paragraph: int = 0) -> dict:
    """Read investigation methods, not case evidence. The planning graph supplies the reader."""
    settings = config["configurable"]
    return settings["skill_directory"].read(settings["skill_catalogue"],
        SkillReadRequest(name=name, resource=resource, start_paragraph=start_paragraph),
        max_tokens=settings.get("body_budget", 6000))


class SkillDirectory:
    """One trusted directory, injected at construction; no executable plugins or hot reload."""

    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.snapshot()  # Fail deployment on malformed or unsafe bundled files.

    def snapshot(self) -> list[dict]:
        catalogue = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            folder = path.parent.resolve()
            if not folder.is_relative_to(self.root):
                raise ValueError("Skill directory escapes deployment root")
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) != 3 or parts[0].strip():
                raise ValueError(f"Invalid skill frontmatter: {path.name}")
            metadata = yaml.safe_load(parts[1])
            if not isinstance(metadata, dict) or set(metadata) != {"name", "description"}:
                raise ValueError("Skills require exactly name and description")
            if (metadata["name"] != path.parent.name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", path.parent.name)
                    or not isinstance(metadata["description"], str) or not metadata["description"].strip()):
                raise ValueError("Invalid skill name or description")
            files = {}
            for resource in [path, *sorted(path.parent.glob("references/**/*.md"))]:
                resolved = resource.resolve(strict=True)
                if not resolved.is_relative_to(folder) or not resolved.is_file():
                    raise ValueError("Skill resource escapes its directory")
                data = resolved.read_bytes()
                if len(data) > 65536:
                    raise ValueError("Skill resource exceeds 64 KiB")
                files[resource.relative_to(path.parent).as_posix()] = hashlib.sha256(data).hexdigest()
            catalogue.append(dict(**metadata, files=files))
        if len({item["name"] for item in catalogue}) != len(catalogue):
            raise ValueError("Duplicate skill name")
        return catalogue

    def read(self, catalogue, request: SkillReadRequest, *, max_tokens=6000) -> dict:
        entry = next((item for item in catalogue if item["name"] == request.name), None)
        if entry is None or request.resource not in entry["files"]:
            return {"error": "SKILL_RESOURCE_NOT_ALLOWED"}
        folder = (self.root / request.name).resolve()
        path = (folder / request.resource).resolve()
        if not folder.is_relative_to(self.root) or not path.is_relative_to(folder):
            return {"error": "SKILL_RESOURCE_NOT_ALLOWED"}
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return {"error": "SKILL_RESOURCE_MISSING"}
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["files"][request.resource]:
            # A deployment changed. Do not let the model work around mixed versions.
            raise ValueError("SKILL_DEPLOYMENT_CHANGED")
        content = data.decode("utf-8")
        page = paragraph_page(content, start=request.start_paragraph, max_tokens=max_tokens)
        return dict(name=request.name, resource=request.resource, digest=digest, **page,
                    kind="METHOD_NOT_CASE_EVIDENCE")


# digest的作用：本轮规划开始：
# psi.md 内容为 A → 计算指纹，保存进本轮目录
# 模型稍后请求读取：
# 重新读取 psi.md → 再计算指纹 → 与保存值比较
