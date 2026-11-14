import json
from pydantic import BaseModel, Field
from typing import override
from config import ROOT_DIR, SKILLS_DIR
from langchain_core.tools import BaseTool
from agent.tools.pub_base import sort_skills, skill_visible_to


relative_path = SKILLS_DIR.relative_to(ROOT_DIR)


class SkillListSchema(BaseModel):
    category: str | None = Field(
        description="Optional category filter to narrow results.", default=None
    )


class SkillList(BaseTool):
    name: str = "skill_list"
    description: str = (
        "List available skills (name + description). Use skill_view(name) to load full content."
    )
    args_schema: type[BaseModel] = SkillListSchema
    metadata: dict = {"idempotent": True}

    @override
    def _run(self, category: str | None):
        try:
            if not SKILLS_DIR.exists():
                return json.dumps(
                    {
                        "success": True,
                        "skills": [],
                        "categories": [],
                        "message": f"Skills directory not found at {relative_path}",
                    },
                    ensure_ascii=False,
                )

            from skills.loader import scan_skills

            all_skills = scan_skills(use_cache=False)

            if not all_skills:
                return json.dumps(
                    {
                        "success": True,
                        "skills": [],
                        "categories": [],
                        "message": f"No skills found in {relative_path} directory.",
                    },
                    ensure_ascii=False,
                )

            # Scope + role visibility: subagent-stamped tool instances carry
            # metadata["caller_scope"] = "subagent" and (for a specialized
            # functional role) metadata["caller_role"], both set by the spawn
            # tool policy. They must not see skills scoped "main_only" or
            # tagged with a ``roles: [...]`` list that omits their role. Absent
            # metadata (main agent / identity role) defaults to "main" / None —
            # unchanged behavior.
            metadata = getattr(self, "metadata", None)
            caller_scope = (
                metadata.get("caller_scope", "main") if isinstance(metadata, dict) else "main"
            )
            caller_role = metadata.get("caller_role") if isinstance(metadata, dict) else None
            all_skills = [s for s in all_skills if skill_visible_to(s, caller_scope, caller_role)]

            # Filter by category if specified
            if category:
                all_skills = [s for s in all_skills if s.get("category") == category]

            # Sort by category then name
            all_skills = sort_skills(all_skills)

            # Extract unique categories
            categories = sorted({s.get("category") for s in all_skills if s.get("category")})

            return json.dumps(
                {
                    "success": True,
                    "skills": all_skills,
                    "categories": categories,
                    "count": len(all_skills),
                    "hint": "Use skill_view(name) to see full content, tags, and linked files",
                },
                ensure_ascii=False,
            )

        except Exception as e:
            return str(e)

    @override
    async def _arun(self, category: str | None):
        return self._run(category)


def build_skill_list_tool() -> BaseTool:
    tool: BaseTool = SkillList()
    tool.handle_tool_error = True
    return tool
