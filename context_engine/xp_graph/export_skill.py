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
from context_engine.xp_graph.type import GmNode, GmEdge


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


def export_all_skills(role: str = "default") -> list[Path]:
    """Export all SKILL nodes from the XpGraph database as SKILL.md files.

    Args:
        role: DB role ('default' or 'worker').

    Returns:
        List of paths to the generated SKILL.md files.
    """
    db = get_db(role)
    all_nodes = all_active_nodes(db)
    all_edges_list = all_edges(db)

    # Filter to SKILL nodes only
    skill_nodes = [n for n in all_nodes if n.type == "SKILL"]
    if not skill_nodes:
        logger.info("No SKILL nodes found in DB (role={}).", role)
        return []

    # Build lookup maps
    nodes_by_id: dict[str, GmNode] = {n.id: n for n in all_nodes}
    edges_by_from: dict[str, list[GmEdge]] = {}
    edges_by_to: dict[str, list[GmEdge]] = {}
    for edge in all_edges_list:
        edges_by_from.setdefault(edge.from_id, []).append(edge)
        edges_by_to.setdefault(edge.to_id, []).append(edge)

    generated: list[Path] = []

    for skill in skill_nodes:
        safe_name = normalize_name(skill.name)
        skill_dir = AUTO_SINGLE_DIR / safe_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        md_path = skill_dir / "SKILL.md"

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

        content = frontmatter_yaml + "\n" + body

        md_path.write_text(content, encoding="utf-8")
        generated.append(md_path)
        logger.info("Exported: {} ({})", md_path, safe_name)

    logger.info(
        "Exported {} skill(s) to {} (role={}).",
        len(generated),
        AUTO_SINGLE_DIR,
        role,
    )
    return generated


def export_all_communities(role: str = "default") -> list[Path]:
    """Export community SKILL.md files to skills/auto/community/<community_id>/SKILL.md.

    For each community that has at least one SKILL-type node, generates a
    SKILL.md that lists the name, description, and link to each member skill's
    skills/auto/single/<skill_name>/ directory.

    Args:
        role: DB role ('default' or 'worker').

    Returns:
        List of generated SKILL.md paths.
    """
    db = get_db(role)
    all_nodes = all_active_nodes(db)

    # Build per-community lists of SKILL nodes
    skill_nodes = [n for n in all_nodes if n.type == "SKILL"]
    community_skills: dict[str, list[GmNode]] = {}
    for node in skill_nodes:
        cid = node.community_id
        if cid is None:
            continue
        community_skills.setdefault(cid, []).append(node)

    if not community_skills:
        logger.info("No community-assigned SKILL nodes found (role={}).", role)
        return []

    # Load community summaries for description text
    summaries = get_all_community_summaries(db)
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

        body = "\n\n## Skills\n\n" + "\n".join(member_lines)

        content = frontmatter_yaml + "\n" + body

        md_path.write_text(content, encoding="utf-8")
        generated.append(md_path)
        logger.info("Exported community SKILL.md: {} ({} skills)", cid, len(members))

    logger.info(
        "Exported {} community SKILL.md(s) to {} (role={}).",
        len(generated),
        AUTO_COMMUNITY_DIR,
        role,
    )
    return generated


if __name__ == "__main__":
    export_all_skills()
    export_all_communities()
