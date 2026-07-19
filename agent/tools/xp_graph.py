import json
from pathlib import Path
from loguru import logger
from pydantic import BaseModel, Field
from typing import Annotated, Any
from langchain_core.tools import tool, BaseTool
from langgraph.prebuilt.tool_node import InjectedState
from config import ROOT_DIR, SKILLS_DIR
from context_engine.xp_graph import extract, ExperienceTrace
from agent.tools.pub_base import parse_frontmatter

_AUTO_SINGLE_DIR: Path | None = None
_RELATIVE_PATH = Path("skills/auto/single")


def _get_auto_single_dir() -> Path:
    global _AUTO_SINGLE_DIR
    if _AUTO_SINGLE_DIR is None:
        _AUTO_SINGLE_DIR = SKILLS_DIR / "auto" / "single"
        if _AUTO_SINGLE_DIR.exists():
            _RELATIVE_PATH = _AUTO_SINGLE_DIR.relative_to(ROOT_DIR)
    return _AUTO_SINGLE_DIR


class XPExtractSchema(BaseModel):
    experience_trace: ExperienceTrace = Field(default=None, description="Pre-built structured experience trace to extract into the knowledge graph")
    auto_skill_name: str | None = Field(
        default=None,
        description="Name of an auto-skill (from skills/auto/single/) to view its full content. Omit to use the experience_trace parameter for knowledge graph extraction.",
    )

# ─── Auto‑skill viewer (skills/auto/single/) ─────────────────────────────


def _list_or_view_auto_skill(name: str | None = None) -> str:
    """List all auto‑skills, or view a single one's SKILL.md."""
    auto_dir = _get_auto_single_dir()
    try:
        if not auto_dir.is_dir():
            return json.dumps(
                {"success": True, "skills": [], "message": f"Auto‑skill directory not found at {_RELATIVE_PATH}"},
                ensure_ascii=False,
            )

        # ── List mode ─────────────────────────────────────────────
        if name is None:
            skills: list[dict[str, Any]] = []
            for skill_md in auto_dir.rglob("SKILL.md"):
                if not skill_md.is_file():
                    continue
                content = skill_md.read_text(encoding="utf-8")
                fm, _ = parse_frontmatter(content)
                skills.append({"name": str(fm.get("name", skill_md.parent.name)), "description": str(fm.get("description", ""))})
            skills.sort(key=lambda s: s["name"])
            return json.dumps(
                {"success": True, "skills": skills, "count": len(skills), "hint": "Pass auto_skill_name to view full content."},
                ensure_ascii=False,
            )

        # ── View mode ─────────────────────────────────────────────
        name = name.strip()
        target_md: Path | None = None
        for skill_md in auto_dir.rglob("SKILL.md"):
            if not skill_md.is_file():
                continue
            content = skill_md.read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)
            skill_name = str(fm.get("name", skill_md.parent.name))
            if skill_name == name or skill_md.parent.name == name:
                target_md = skill_md
                break

        if target_md is None:
            available = [str(p.parent.name) for p in auto_dir.rglob("SKILL.md") if p.is_file()]
            return json.dumps({"success": False, "error": f"Auto‑skill '{name}' not found.", "available_skills": available}, ensure_ascii=False)

        full_content = target_md.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(full_content)
        return json.dumps(
            {"success": True, "name": str(fm.get("name", target_md.parent.name)), "description": str(fm.get("description", "")), "content": full_content},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@tool("xp_graph", args_schema=XPExtractSchema)
async def xp_graph_tool(
    session_id: Annotated[str, InjectedState("session_id")] = "",
    experience_trace: ExperienceTrace | None = None,
    auto_skill_name: str | None = None,
) -> str:
    """Extract structured experience into the knowledge graph, or view auto-learned skills.

    Two modes:
    1. Pass `experience_trace` — extracts an ExperienceTrace into the knowledge graph.
    2. Pass `auto_skill_name` (or omit everything) — list or view auto-skills
       from skills/auto/single/. Call without arguments to list all auto-skills;
       pass a name to view the full SKILL.md content."""
    # ── Auto-skill view mode ──────────────────────────────────────
    if experience_trace is None:
        return _list_or_view_auto_skill(auto_skill_name)
    # ── Manual conversion: LLM may pass dict, not ExperienceTrace instance ──
    if experience_trace is not None and not isinstance(experience_trace, ExperienceTrace):
        experience_trace = ExperienceTrace(**experience_trace)

    # ── Knowledge graph extraction mode ───────────────────────────
    try:
        result = await extract(
            experience_trace=experience_trace,
            session_id=session_id,
        )
        node_summary = ", ".join(f"{n.type.value if hasattr(n.type, 'value') else n.type}:{n.name}" for n in result.nodes)
        edge_summary = ", ".join(f"{e.from_id}→{e.to_id}({e.type.value if hasattr(e.type, 'value') else e.type})" for e in result.edges)
        logger.info(
            f"[xp_graph_tool] extracted {len(result.nodes)} nodes [{node_summary}], "
            f"{len(result.edges)} edges [{edge_summary}]"
        )
        return (
            f"Extracted {len(result.nodes)} nodes and {len(result.edges)} edges "
            f"into the experience knowledge graph."
        )
    except Exception as e:
        logger.error(f"[xp_graph_tool] extraction failed: {e}")
        return f"Extraction failed: {e}"


def build_xp_graph_tool() -> BaseTool:
    xp_graph_tool.handle_tool_error = True
    return xp_graph_tool