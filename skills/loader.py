"""Skills loader and snapshot builder."""

import json
import yaml
from pathlib import Path
from typing import Any
from loguru import logger
from config import (
    ROOT_DIR,
    SKILLS_DIR,
    SKILLS_STATE_FILE,
    SKILL_DISCOVERY_ROOTS,
    is_allowed_skill_path,
)


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
        with open(SKILLS_STATE_FILE, encoding="utf-8") as f:
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
    return Path(location).parts[:2] == ("skills", "plugins")


def read_skills_snapshot() -> list[dict[str, str]] | None:
    """Read the cached skills snapshot written by build_skills_snapshot().

    Returns None when the snapshot file does not exist. Lives on the loader
    (its only consumer is scan_skills) so skills_snapshot can depend on the
    loader one-directionally instead of forming an import cycle (audit #18).
    """
    file_path = SKILLS_DIR / "skills_snapshot.json"
    if not file_path.exists():
        return None
    with open(file_path, encoding="utf-8") as f:
        return json.loads(f.read())


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
    if use_cache:
        cached: list[dict[str, str]] | None = read_skills_snapshot()

        if cached:
            return cached

    state = _read_skills_state()

    skills: list[dict[str, Any]] = []
    seen_paths = set()  # for deduplication

    for skill_file in SKILLS_DIR.glob("**/SKILL.md"):
        if not is_allowed_skill_path(skill_file, SKILLS_DIR):
            logger.warning(
                "Ignoring SKILL.md outside allowed roots "
                f"({', '.join(SKILL_DISCOVERY_ROOTS)}): {skill_file}"
            )
            continue
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
    Get the skills XML.
    :param selected_skill_names: list of selected skill names
    :param caller_scope: the caller's perspective ("main" or "subagent"). Skills
        scoped "main_only" are invisible to subagents; "subagent_only" skills are
        invisible to main (see the ``scope:`` frontmatter field; default "all"
        makes them visible to both).
    :return: skills xml
    """
    skills: list[dict[str, Any]] = scan_skills()

    final_skills: list[dict[str, Any]] = []
    if selected_skill_names is not None and len(selected_skill_names) > 0:
        for s in skills:
            if s["name"] in selected_skill_names and _skill_visible_to(s, caller_scope):
                final_skills.append(s)

    # If selected_skill_names is empty, select all by default
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
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
