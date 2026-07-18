"""
Export SKILL-type nodes from the XpGraph database as SKILL.md files.

For each SKILL node in the DB, generates:
  skills/auto/single/<normalized_name>/SKILL.md

SKILL.md format:
  ---
  name: <skill_name>
  description: <skill_description>
  ---
  <body: content + relationship info>

Edge types handled: USED_SKILL, SOLVED_BY, REQUIRES, PATCHES, CONFLICTS_WITH
For each relevant edge, the linked skill's name and description are included
(but NOT the linked skill's full content).
"""

import shutil
from pathlib import Path
from loguru import logger

from config.path import SKILLS_DIR
from context_engine.xp_graph.store.core import (
    all_active_nodes,
    all_edges,
    normalize_name,
    get_all_community_summaries,
)
from context_engine.xp_graph.store.db import get_db
from context_engine.xp_graph.type import GmNode, GmEdge, NodeType


# ─── Edge types that link SKILL to SKILL ──────────────────────────
_SKILL_EDGE_TYPES = frozenset({
    "USED_SKILL",
    "SOLVED_BY",
    "REQUIRES",
    "PATCHES",
    "CONFLICTS_WITH",
})

# ─── Output directories ───────────────────────────────────────────
AUTO_SINGLE_DIR = SKILLS_DIR / "auto" / "single"
AUTO_COMMUNITY_DIR = SKILLS_DIR / "auto" / "community"


def _clean_dir(directory: Path) -> None:
    """Remove and recreate an empty directory."""
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)


def _edge_type_match(edge_type_val: str) -> bool:
    """Check if an edge type value is in the SKILL-to-SKILL set."""
    return edge_type_val in _SKILL_EDGE_TYPES


def _build_relations(
    skill_node: GmNode,
    nodes_by_id: dict[str, GmNode],
    edges_by_from: dict[str, list[GmEdge]],
    edges_by_to: dict[str, list[GmEdge]],
) -> list[dict[str, object]]:
    """Collect all relevant relations for a skill node.

    For each edge where the skill is either `from_id` or `to_id`
    and the edge type is in _SKILL_EDGE_TYPES, resolve the peer skill
    node's name + description.
    """
    relations: list[dict[str, object]] = []

    # Outgoing edges (SKILL → other)
    for edge in edges_by_from.get(skill_node.id, []):
        if not _edge_type_match(edge.type.value):
            continue
        peer_node = nodes_by_id.get(edge.to_id)
        if peer_node is None:
            continue
        relations.append({
            "direction": "outgoing",
            "edge_type": edge.type.value,
            "edge_instruction": edge.instruction or "",
            "edge_condition": edge.condition or "",
            "peer_name": peer_node.name,
            "peer_description": peer_node.description,
        })

    # Incoming edges (other → SKILL)
    for edge in edges_by_to.get(skill_node.id, []):
        if not _edge_type_match(edge.type.value):
            continue
        peer_node = nodes_by_id.get(edge.from_id)
        if peer_node is None:
            continue
        relations.append({
            "direction": "incoming",
            "edge_type": edge.type.value,
            "edge_instruction": edge.instruction or "",
            "edge_condition": edge.condition or "",
            "peer_name": peer_node.name,
            "peer_description": peer_node.description,
        })

    return relations


def _format_relations_section(relations: list[dict[str, object]]) -> str:
    """Format the relations section of the SKILL.md body."""
    if not relations:
        return ""

    lines = ["\n## Relations\n"]
    for rel in relations:
        direction = rel["direction"]
        arrow = "→" if direction == "outgoing" else "←"
        lines.append(
            f"- **{rel['edge_type']}** {arrow} **{rel['peer_name']}**: "
            f"{rel['peer_description']}"
        )
        instruction = rel.get("edge_instruction", "")
        condition = rel.get("edge_condition", "")
        if instruction:
            lines.append(f"  - Instruction: {instruction}")
        if condition:
            lines.append(f"  - Condition: {condition}")
        lines.append("")

    return "\n".join(lines)


def _build_skill_md(
    skill: GmNode,
    nodes_by_id: dict[str, GmNode],
    edges_by_from: dict[str, list[GmEdge]],
    edges_by_to: dict[str, list[GmEdge]],
) -> str:
    """Build SKILL.md content for a single skill node."""
    safe_name = normalize_name(skill.name)

    # Collect relations
    relations = _build_relations(skill, nodes_by_id, edges_by_from, edges_by_to)

    # ── Build SKILL.md content ──────────────────────────────────
    frontmatter_yaml = (
        f"---\n"
        f"name: {safe_name}\n"
        f"description: {skill.description or skill.name}\n"
        f"---\n"
    )

    body_parts: list[str] = []
    if skill.content.strip():
        body_parts.append(skill.content)

    rel_section = _format_relations_section(relations)
    if rel_section:
        body_parts.append(rel_section)

    meta_lines = [
        "<!--",
        f"  validated_count: {skill.validated_count}",
        f"  source_sessions: {len(skill.source_sessions)}",
        f"  created_at: {skill.created_at}",
        f"  updated_at: {skill.updated_at}",
        "-->",
    ]
    body_parts.append("\n" + "\n".join(meta_lines))

    body = "\n\n".join(body_parts)

    return frontmatter_yaml + "\n" + body


def _write_skill_md(output_dir: Path, skill: GmNode, content: str) -> Path:
    """Write a single SKILL.md file under the given output directory."""
    safe_name = normalize_name(skill.name)
    skill_dir = output_dir / safe_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / "SKILL.md"
    md_path.write_text(content, encoding="utf-8")
    logger.info("Exported: {} ({})", md_path, safe_name)
    return md_path


def export_all_skills() -> list[Path]:
    """Export all SKILL nodes from the XpGraph database as SKILL.md files.

    Cleans the skills/auto/single/ directory first, then regenerates all files.

    Returns:
        List of paths to the generated SKILL.md files.
    """
    db = get_db()
    all_nodes = all_active_nodes(db)
    all_edges_list = all_edges(db)

    # Filter to SKILL nodes only
    skill_nodes = [n for n in all_nodes if n.type == NodeType.SKILL]
    if not skill_nodes:
        logger.info("No SKILL nodes found in DB.")
        return []

    # Clean output directory
    _clean_dir(AUTO_SINGLE_DIR)

    # Build lookup maps
    nodes_by_id: dict[str, GmNode] = {n.id: n for n in all_nodes}
    edges_by_from: dict[str, list[GmEdge]] = {}
    edges_by_to: dict[str, list[GmEdge]] = {}
    for edge in all_edges_list:
        edges_by_from.setdefault(edge.from_id, []).append(edge)
        edges_by_to.setdefault(edge.to_id, []).append(edge)

    generated: list[Path] = []

    for skill in skill_nodes:
        content = _build_skill_md(skill, nodes_by_id, edges_by_from, edges_by_to)
        md_path = _write_skill_md(AUTO_SINGLE_DIR, skill, content)
        generated.append(md_path)

    logger.info(
        "Exported {} skill(s) to {}.",
        len(generated),
        AUTO_SINGLE_DIR,
    )
    return generated


def export_all_communities() -> list[Path]:
    """Export community SKILL.md files to skills/auto/community/<community_id>/SKILL.md.

    For each community that has at least one SKILL-type node, generates a
    SKILL.md that lists the name, description, and link to each member skill's
    skills/auto/single/<skill_name>/ directory.

    When no communities exist yet (gm_communities table is empty), falls back
    to exporting individual SKILL.md files under skills/auto/community/ as a
    temporary stand-in.

    Both paths always clean the skills/auto/community/ directory first.

    Returns:
        List of generated SKILL.md paths.
    """
    db = get_db()

    # Always clean output directory first
    _clean_dir(AUTO_COMMUNITY_DIR)

    # ── Fallback: no communities yet → export individual skills ──────
    summaries = get_all_community_summaries(db)
    if not summaries:
        logger.info("gm_communities table is empty — falling back to individual skill export under community/.")
        all_nodes = all_active_nodes(db)
        all_edges_list = all_edges(db)

        skill_nodes = [n for n in all_nodes if n.type == NodeType.SKILL]
        if not skill_nodes:
            logger.info("No SKILL nodes found in DB.")
            return []

        nodes_by_id: dict[str, GmNode] = {n.id: n for n in all_nodes}
        edges_by_from: dict[str, list[GmEdge]] = {}
        edges_by_to: dict[str, list[GmEdge]] = {}
        for edge in all_edges_list:
            edges_by_from.setdefault(edge.from_id, []).append(edge)
            edges_by_to.setdefault(edge.to_id, []).append(edge)

        generated: list[Path] = []
        for skill in skill_nodes:
            content = _build_skill_md(skill, nodes_by_id, edges_by_from, edges_by_to)
            md_path = _write_skill_md(AUTO_COMMUNITY_DIR, skill, content)
            generated.append(md_path)

        logger.info(
            "Exported {} skill(s) to {} (community fallback).",
            len(generated),
            AUTO_COMMUNITY_DIR,
        )
        return generated

    all_nodes = all_active_nodes(db)

    # Build per-community lists of SKILL nodes
    skill_nodes = [n for n in all_nodes if n.type == NodeType.SKILL]
    community_skills: dict[str, list[GmNode]] = {}
    for node in skill_nodes:
        cid = node.community_id
        if cid is None:
            continue
        community_skills.setdefault(cid, []).append(node)

    if not community_skills:
        logger.info("No community-assigned SKILL nodes found.")
        return []

    # Load community summaries for description text
    summary_map: dict[str, str] = {s["id"]: s["summary"] for s in summaries}

    generated: list[Path] = []

    for cid, members in sorted(community_skills.items()):
        community_dir = AUTO_COMMUNITY_DIR / cid
        community_dir.mkdir(parents=True, exist_ok=True)
        md_path = community_dir / "SKILL.md"

        description = summary_map.get(cid, f"Community {cid}")

        # ── Build SKILL.md content ──────────────────────────────────
        frontmatter_yaml = (
            f"---\n"
            f"name: community_{cid}\n"
            f"description: {description}\n"
            f"---\n"
        )

        members.sort(key=lambda n: n.name)
        member_lines: list[str] = []
        for m in members:
            safe_name = normalize_name(m.name)
            link = f"skills/auto/single/{safe_name}/"
            member_lines.append(f"- [{m.name}]({link}): {m.description}")

        note = (
            "\n\n> **Note:** When this community directory contains SKILL.md files, the unified skill scanner\n"
            "> only reads from community/ and skips skills/auto/single/. To view the full content of\n"
            "> any skill listed below, use the `xp_graph` tool with `auto_skill_name=\"<skill_name>\"`."
        )

        body = "\n\n## Skills\n\n" + "\n".join(member_lines) + note

        content = frontmatter_yaml + "\n" + body

        md_path.write_text(content, encoding="utf-8")
        generated.append(md_path)
        logger.info("Exported community SKILL.md: {} ({} skills)", cid, len(members))

    logger.info(
        "Exported {} community SKILL.md(s) to {}.",
        len(generated),
        AUTO_COMMUNITY_DIR,
    )
    return generated


# ─── Auto skill scanning (community-first fallback) ──────────────


def scan_auto_skills() -> list[dict[str, str]]:
    """Scan auto-learned skills with community-first fallback.

    If ``skills/auto/community/`` contains any ``SKILL.md`` files, only
    those are returned.  Otherwise the function falls back to scanning
    ``skills/auto/single/``.

    Returns:
        A list of ``{"name": ..., "description": ...}`` dicts sorted by name.
    """
    from skills.loader import parse_frontmatter

    # ── Determine which base dir to use ────────────────────────────────
    if AUTO_COMMUNITY_DIR.exists() and list(AUTO_COMMUNITY_DIR.rglob("SKILL.md")):
        base_dir = AUTO_COMMUNITY_DIR
    else:
        base_dir = AUTO_SINGLE_DIR

    if not base_dir.exists():
        return []

    skills: list[dict[str, str]] = []
    for skill_file in base_dir.rglob("SKILL.md"):
        # Only pick up direct children: <base_dir>/<skill_name>/SKILL.md
        # (skip nested community/xxx/skills/… etc.)
        if skill_file.parent.parent != base_dir:
            continue

        content = skill_file.read_text(encoding="utf-8")
        try:
            meta = parse_frontmatter(content)
        except Exception:
            meta = {}
        name = str(meta.get("name", skill_file.parent.name))
        description = str(meta.get("description", ""))
        skills.append({"name": name, "description": description})

    skills.sort(key=lambda x: x["name"])
    return skills


if __name__ == "__main__":
    export_all_skills()
    export_all_communities()
