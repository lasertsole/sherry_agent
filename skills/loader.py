"""Skills loader and snapshot builder."""

import json
import yaml
from typing import Any
from config import ROOT_DIR, SKILLS_DIR, AUTO_SKILLS_DIR, PLUGIN_SKILLS_DIR, SKILLS_STATE_FILE


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def _read_skills_state() -> dict[str, dict[str, bool]]:
    """Read the skills state file defensively.

    Returns a mapping of skill name -> {"active": bool}. Missing or malformed
    files degrade to an empty dict.
    """
    try:
        if not SKILLS_STATE_FILE.exists():
            return {}
        with open(SKILLS_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict[str, bool]] = {}
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(value.get("active"), bool):
                result[str(key)] = {"active": value["active"]}
        return result
    except (OSError, json.JSONDecodeError):
        return {}


def _is_third_party(location: str) -> bool:
    """Return True if the skill location is under skills/plugins/."""
    return "./skills/plugins/" in location


def scan_skills(use_cache: bool = True) -> list[dict[str, Any]]:
    from .skills_snapshot import read_skills_snapshot

    if use_cache:
        skills_snapshot: list[dict[str, str]] | None = read_skills_snapshot()

        if skills_snapshot:
            return skills_snapshot

    state = _read_skills_state()

    skills: list[dict[str, Any]] = []
    seen_paths = set()  # 用于去重

    for skill_file in SKILLS_DIR.glob("**/SKILL.md"):
        if skill_file in seen_paths:
            continue
        seen_paths.add(skill_file)

        content = skill_file.read_text(encoding="utf-8")
        meta = parse_frontmatter(content)
        name = str(meta.get("name", skill_file.parent.name))
        desc = str(meta.get("description", ""))
        rel = skill_file.relative_to(ROOT_DIR)
        location = f"./{rel.as_posix()}"

        if _is_third_party(location):
            # Uploaded third-party skills default to inactive unless the state
            # file explicitly marks them active.
            active = bool(state.get(name, {}).get("active", False))
        else:
            # Builtin / auto skills are always active.
            active = True

        skills.append(
            {
                "name": name,
                "description": desc,
                "location": location,
                "active": active,
            }
        )

    skills.sort(key=lambda x: x["name"])
    return skills


def get_skills_text(selected_skill_names: list[str] | None = None, exclude_auth_skills: bool | None = None) -> str:
    """
    获取 skills xml
    :param selected_skill_names: 选中的技能名字列表
    :param exclude_auth_skills: 是否排除高权限技能
    :return: skills xml
    """
    skills: list[dict[str, Any]] = scan_skills()

    exclude_skill_names: list[str] = []
    if exclude_auth_skills is not None and exclude_auth_skills:
        exclude_skill_names = ["clawhub", "skill_creator"]

    final_skills: list[dict[str, Any]] = []
    if selected_skill_names is not None and len(selected_skill_names) > 0:
        for s in skills:
            if s["name"] in selected_skill_names and s["name"] not in exclude_skill_names:
                final_skills.append(s)

    # 如果selected_skill_names为空则默认全选
    else:
        for s in skills:
            if s["name"] not in exclude_skill_names:
                final_skills.append(s)

    # Filter out inactive skills (uploaded third-party skills default to inactive).
    final_skills = [s for s in final_skills if s.get("active", True)]

    lines = ["<available_skills>"]
    for s in final_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{s['name']}</name>")
        lines.append(f"    <description>{s['description']}</description>")
        lines.append(f"    <location>{s['location']}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
