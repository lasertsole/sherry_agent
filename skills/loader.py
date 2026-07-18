"""Skills loader and snapshot builder."""

import yaml
from typing import Any
from config import ROOT_DIR, SKILLS_DIR, AUTO_SKILLS_DIR


def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    return yaml.safe_load(parts[1]) or {}


def scan_skills(use_cache: bool = True) -> list[dict[str, Any]]:
    """Scan all SKILL.md files under *SKILLS_DIR*, delegating the ``auto/``
    sub-tree to :func:`scan_auto_skills` which applies community-first
    fallback logic.
    """
    from pathlib import Path

    # Lazy import to avoid cycle: loader → export_skill → loader
    from context_engine.xp_graph.export_skill import scan_auto_skills

    skills: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    # 1. Delegate auto/ scanning to the unified function (community-first)
    skills.extend(scan_auto_skills())

    # 2. Scan the rest of SKILLS_DIR (built-in, plugins, testing, …)
    #    skipping auto/ to avoid double-counting.
    auto_dir_str = AUTO_SKILLS_DIR.resolve().as_posix()

    for skill_file in SKILLS_DIR.glob("**/SKILL.md"):
        if skill_file in seen_paths:
            continue

        # Skip files under auto/ — already handled above
        if skill_file.resolve().as_posix().startswith(auto_dir_str):
            continue

        seen_paths.add(skill_file)

        content = skill_file.read_text(encoding="utf-8")
        meta = parse_frontmatter(content)
        name = str(meta.get("name", skill_file.parent.name))
        desc = str(meta.get("description", ""))
        skills.append(
            {
                "name": name,
                "description": desc,
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

    lines = ["<available_skills>"]
    for s in final_skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{s['name']}</name>")
        lines.append(f"    <description>{s['description']}</description>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)
