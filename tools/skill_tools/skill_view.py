import json
from pathlib import Path
from loguru import logger
from typing import Any, Type
from config import AUTO_SKILLS_DIR
from pydantic import BaseModel, Field
from typing_extensions import override
from langchain_core.tools import BaseTool
from pathlib import PurePosixPath, PureWindowsPath
from tools.pub_base import (
    sort_skills,
    find_auto_skills,
    parse_frontmatter,
    iter_skill_index_files,
    skill_matches_platform,
    EXCLUDED_SKILL_DIRS,
)

_MAX_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024
_ALLOWED_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))

_INJECTION_PATTERNS: list[str] = [
    "ignore previous instructions",
    "ignore all previous",
    "you are now",
    "disregard your",
    "forget your instructions",
    "new instructions:",
    "system prompt:",
    "<system>",
    "]]>",
]


def _skill_lookup_path_error(name: str) -> str | None:
    from pub_func import has_traversal_component

    if not isinstance(name, str):
        return "Skill name must be a string."
    candidate = name.strip()
    if (
        PurePosixPath(candidate).is_absolute()
        or PureWindowsPath(candidate).is_absolute()
        or PureWindowsPath(candidate).drive
    ):
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def _is_skill_support_path(path: str | Path) -> bool:
    """Check if a path is under a skill support directory (references/templates/assets/scripts)."""
    try:
        parts = Path(path).parts if not isinstance(path, PurePosixPath) else path.parts
    except Exception:
        return False
    if not parts:
        return False
    first = parts[0].lower() if isinstance(parts[0], str) else str(parts[0]).lower()
    return first in _ALLOWED_SUPPORT_DIRS


def _parse_tags(tags_value) -> list[str]:
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _get_category_from_path(skill_path) -> str | None:
    try:
        rel_path = skill_path.relative_to(AUTO_SKILLS_DIR)
        parts = rel_path.parts
        if len(parts) >= 3:
            return parts[0]
    except (ValueError, Exception):
        pass
    return None


def _skill_view(name: str, file_path: str | None = None) -> str:
    try:
        lookup_error = _skill_lookup_path_error(name)
        if lookup_error:
            return json.dumps(
                {
                    "success": False,
                    "error": lookup_error,
                    "hint": "Use a skill name or relative path within the skills directory.",
                },
                ensure_ascii=False,
            )

        if not AUTO_SKILLS_DIR.exists():
            return json.dumps(
                {
                    "success": False,
                    "error": "Skills directory does not exist yet.",
                },
                ensure_ascii=False,
            )

        skill_dir = None
        skill_md = None

        candidates: list[tuple[Any | None, Any]] = []
        seen_md: set = set()

        def _record(sd, smd):
            try:
                key = smd.resolve()
            except Exception:
                key = smd
            if key in seen_md:
                return
            seen_md.add(key)
            candidates.append((sd, smd))

        # Strategy 1: direct path
        direct_path = AUTO_SKILLS_DIR / name
        if (
            not _is_skill_support_path(direct_path)
            and direct_path.is_dir()
            and (direct_path / "SKILL.md").exists()
        ):
            _record(direct_path, direct_path / "SKILL.md")
        elif direct_path.with_suffix(".md").exists() and not _is_skill_support_path(
            direct_path.with_suffix(".md")
        ):
            _record(None, direct_path.with_suffix(".md"))

        # Strategy 2: recursive by directory name + frontmatter name
        for found_skill_md in iter_skill_index_files(AUTO_SKILLS_DIR, "SKILL.md"):
            if any(part in EXCLUDED_SKILL_DIRS for part in found_skill_md.parts):
                continue
            if found_skill_md.parent.name == name:
                _record(found_skill_md.parent, found_skill_md)
                continue
            try:
                fm_content = found_skill_md.read_text(encoding="utf-8")[:4000]
                fm, _ = parse_frontmatter(fm_content)
            except Exception:
                fm = {}
            if fm.get("name") == name:
                _record(found_skill_md.parent, found_skill_md)

        # Strategy 3: legacy flat <name>.md
        for found_md in AUTO_SKILLS_DIR.rglob(f"{name}.md"):
            if found_md.name != "SKILL.md" and not _is_skill_support_path(found_md):
                _record(None, found_md)

        if len(candidates) > 1:
            paths = [str(smd) for _, smd in candidates]
            logger.warning(
                "Skill name collision for '{}': {} candidates — {}",
                name, len(candidates), "; ".join(paths),
            )
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"Ambiguous skill name '{name}': {len(candidates)} skills "
                        "match. Refusing to guess — load one explicitly by its categorized path."
                    ),
                    "matches": paths,
                    "hint": "Pass the full relative path instead of the bare name.",
                },
                ensure_ascii=False,
            )

        if candidates:
            skill_dir, skill_md = candidates[0]

        if not skill_md or not skill_md.exists():
            available = [s["name"] for s in sort_skills(find_auto_skills())[:20]]
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' not found.",
                    "available_skills": available,
                    "hint": "Use skill_list to see all available skills",
                },
                ensure_ascii=False,
            )

        try:
            content = skill_md.read_text(encoding="utf-8")
        except Exception as e:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Failed to read skill '{name}': {e}",
                },
                ensure_ascii=False,
            )

        # Security: detect common prompt injection patterns
        _content_lower = content.lower()
        _injection_detected = any(p in _content_lower for p in _INJECTION_PATTERNS)

        if _injection_detected:
            logger.warning(
                "Skill content for '%s' contains patterns that may indicate prompt injection",
                name,
            )

        frontmatter: dict[str, Any] = {}
        try:
            frontmatter, _ = parse_frontmatter(content)
        except Exception:
            frontmatter = {}

        if not skill_matches_platform(frontmatter):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Skill '{name}' is not supported on this platform.",
                },
                ensure_ascii=False,
            )

        # If a specific file path is requested, read that instead
        if file_path and skill_dir:
            from pub_func import has_traversal_component, validate_within_dir

            if has_traversal_component(file_path):
                return json.dumps(
                    {
                        "success": False,
                        "error": "Path traversal ('..') is not allowed.",
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            target_file = skill_dir / file_path

            traversal_error = validate_within_dir(target_file, skill_dir)
            if traversal_error:
                return json.dumps(
                    {
                        "success": False,
                        "error": traversal_error,
                        "hint": "Use a relative path within the skill directory",
                    },
                    ensure_ascii=False,
                )

            if not target_file.exists():
                available_files: dict[str, list[str]] = {
                    "references": [],
                    "templates": [],
                    "assets": [],
                    "scripts": [],
                    "other": [],
                }

                for f in skill_dir.rglob("*"):
                    if f.is_file() and f.name != "SKILL.md":
                        rel = str(f.relative_to(skill_dir))
                        if rel.startswith("references/"):
                            available_files["references"].append(rel)
                        elif rel.startswith("templates/"):
                            available_files["templates"].append(rel)
                        elif rel.startswith("assets/"):
                            available_files["assets"].append(rel)
                        elif rel.startswith("scripts/"):
                            available_files["scripts"].append(rel)
                        elif f.suffix in {
                            ".md", ".py", ".yaml", ".yml",
                            ".json", ".tex", ".sh",
                        }:
                            available_files["other"].append(rel)

                available_files = {k: v for k, v in available_files.items() if v}

                return json.dumps(
                    {
                        "success": False,
                        "error": f"File '{file_path}' not found in skill '{name}'.",
                        "available_files": available_files,
                        "hint": "Use one of the available file paths listed above",
                    },
                    ensure_ascii=False,
                )

            try:
                file_content = target_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return json.dumps(
                    {
                        "success": True,
                        "name": name,
                        "file": file_path,
                        "content": f"[Binary file: {target_file.name}, size: {target_file.stat().st_size} bytes]",
                        "is_binary": True,
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "success": True,
                    "name": name,
                    "file": file_path,
                    "content": file_content,
                    "file_type": target_file.suffix,
                },
                ensure_ascii=False,
            )

        # ── Main SKILL.md content ──────────────────────────────────────

        # Get linked files
        reference_files = []
        template_files = []
        asset_files = []
        script_files = []

        if skill_dir:
            references_dir = skill_dir / "references"
            if references_dir.exists():
                reference_files = [
                    str(f.relative_to(skill_dir)) for f in references_dir.glob("*.md")
                ]

            templates_dir = skill_dir / "templates"
            if templates_dir.exists():
                for ext in ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"]:
                    template_files.extend(
                        [str(f.relative_to(skill_dir)) for f in templates_dir.rglob(ext)]
                    )

            assets_dir = skill_dir / "assets"
            if assets_dir.exists():
                for f in assets_dir.rglob("*"):
                    if f.is_file():
                        asset_files.append(str(f.relative_to(skill_dir)))

            scripts_dir = skill_dir / "scripts"
            if scripts_dir.exists():
                for ext in ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"]:
                    script_files.extend(
                        [str(f.relative_to(skill_dir)) for f in scripts_dir.glob(ext)]
                    )

        # Read tags / related_skills
        hermes_meta = {}
        metadata = frontmatter.get("metadata")
        if isinstance(metadata, dict):
            hermes_meta = metadata.get("hermes", {}) or {}

        tags = _parse_tags(hermes_meta.get("tags") or frontmatter.get("tags", ""))
        related_skills = _parse_tags(
            hermes_meta.get("related_skills") or frontmatter.get("related_skills", "")
        )

        linked_files = {}
        if reference_files:
            linked_files["references"] = reference_files
        if template_files:
            linked_files["templates"] = template_files
        if asset_files:
            linked_files["assets"] = asset_files
        if script_files:
            linked_files["scripts"] = script_files

        try:
            rel_path = str(skill_md.relative_to(AUTO_SKILLS_DIR))
        except ValueError:
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name

        skill_name = frontmatter.get(
            "name", skill_md.stem if not skill_dir else skill_dir.name
        )

        description = frontmatter.get("description", "")
        if len(description) > _MAX_DESCRIPTION_LENGTH:
            description = description[:_MAX_DESCRIPTION_LENGTH - 3] + "..."

        result = {
            "success": True,
            "name": skill_name,
            "description": description,
            "tags": tags,
            "related_skills": related_skills,
            "content": content,
            "path": rel_path,
            "skill_dir": str(skill_dir) if skill_dir else None,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": (
                "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
                if linked_files
                else None
            ),
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


class SkillViewSchema(BaseModel):
    name: str = Field(
        description="The skill name (use skill_list to see available skills). For plugin-provided skills, use the qualified form 'plugin:skill' (e.g. 'superpowers:writing-plans')."
    )
    file_path: str | None = Field(
        description="OPTIONAL: Path to a linked file within the skill (e.g., 'references/api.md', 'templates/config.yaml', 'scripts/validate.py'). Omit to get the main SKILL.md content.",
        default=None,
    )


class SkillView(BaseTool):
    name: str = "skill_view"
    description: str = (
        "Skills allow for loading information about specific tasks and workflows, as well as scripts and templates. "
        "Load a skill's full content or access its linked files (references, templates, scripts). "
        "First call returns SKILL.md content plus a 'linked_files' dict showing available references/templates/scripts. "
        "To access those, call again with file_path parameter."
    )
    args_schema: Type[BaseModel] = SkillViewSchema

    @override
    def _run(self, name: str, file_path: str | None = None) -> str:
        return _skill_view(name, file_path)

    @override
    async def _arun(self, name: str, file_path: str | None = None) -> str:
        return self._run(name, file_path)


def build_skill_view_tool() -> BaseTool:
    tool: BaseTool = SkillView()
    tool.handle_tool_error = True
    return tool
