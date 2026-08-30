"""Skills loader and snapshot builder."""

import json
import yaml
from typing import Any
from config import ROOT_DIR, SKILLS_DIR, SKILLS_STATE_FILE


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


# Scope visibility values for the ``scope:`` frontmatter field. The canonical
# implementation lives in agent/tools/pub_base/skill_utils.py
# (normalize_skill_scope / skill_visible_to). It is deliberately duplicated
# here as a tiny helper instead of imported: skill_utils imports skills.loader
# at module level (circular import), and importing anything from the
# agent.tools package would drag its heavyweight __init__ (full tool registry
# + langchain) into the lightweight `skills` package import chain.
_SKILL_SCOPES = ("all", "main_only", "subagent_only")


def _normalize_scope(raw: Any) -> str:
    """Normalize a ``scope:`` frontmatter value; invalid/absent -> "all"."""
    if raw is None:
        return "all"
    value = str(raw).strip().lower()
    return value if value in _SKILL_SCOPES else "all"


def _skill_visible_to(skill: dict[str, Any], caller_scope: str) -> bool:
    """Return True when *skill* is visible to a caller with *caller_scope*.

    Visibility contract: "main" sees skills whose scope != "subagent_only";
    "subagent" sees skills whose scope != "main_only". Unknown caller scopes
    degrade to "main"; unknown skill scopes default to "all" (both callers).

    Canonical implementation: agent/tools/pub_base/skill_utils.py
    (see the comment on _SKILL_SCOPES above for why this is duplicated).
    """
    scope = _normalize_scope(skill.get("scope") if isinstance(skill, dict) else None)
    caller = str(caller_scope or "").strip().lower()
    if caller not in ("main", "subagent"):
        caller = "main"
    if caller == "subagent":
        return scope != "main_only"
    return scope != "subagent_only"


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
        scope = _normalize_scope(meta.get("scope"))
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
                "scope": scope,
                "active": active,
            }
        )

    skills.sort(key=lambda x: x["name"])
    return skills


def get_skills_text(
    selected_skill_names: list[str] | None = None,
    caller_scope: str = "main",
) -> str:
    """
    获取 skills xml
    :param selected_skill_names: 选中的技能名字列表
    :param caller_scope: 调用方视角（"main" 或 "subagent"）。scope 为
        "main_only" 的技能对 subagent 不可见，"subagent_only" 的技能对 main
        不可见（见 ``scope:`` frontmatter 字段；默认 "all" 双方可见）。
    :return: skills xml
    """
    skills: list[dict[str, Any]] = scan_skills()

    final_skills: list[dict[str, Any]] = []
    if selected_skill_names is not None and len(selected_skill_names) > 0:
        for s in skills:
            if s["name"] in selected_skill_names and _skill_visible_to(s, caller_scope):
                final_skills.append(s)

    # 如果selected_skill_names为空则默认全选
    else:
        for s in skills:
            if _skill_visible_to(s, caller_scope):
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
