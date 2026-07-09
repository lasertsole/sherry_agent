"""
xp_graph — Assemble Context
"""

import math
from typing import TypedDict
from datetime import datetime
from sqlite3 import Connection
from pub_func import escape_xml
from ..type import GmNode, GmEdge, NodeType
from ..store.core import get_community_summary


CHARS_PER_TOKEN = 3


class SelectedNode(TypedDict):
    """A selected node with its type"""
    type: str

class AssembleResult(TypedDict):
    """Assembly result with XML, system prompt, and token count"""
    xml: str | None
    system_prompt: str
    tokens: int


def build_system_prompt_addition(selected_nodes: list[SelectedNode], edge_count: int) -> str:
    """
    Build the system prompt addition for the knowledge graph

    Args:
        selected_nodes: List of selected nodes
        edge_count: Number of edges

    Returns:
        System prompt addition text
    """
    if not selected_nodes:
        return ""

    skill_count = sum(1 for n in selected_nodes if n['type'] == 'SKILL')
    event_count = sum(1 for n in selected_nodes if n['type'] == 'EVENT')
    task_count = sum(1 for n in selected_nodes if n['type'] == 'TASK')
    is_rich = len(selected_nodes) >= 4 or edge_count >= 3

    sections = [
        "## Graph Memory",
        "",
        "Below `<knowledge_graph>` is your accumulated experience from past conversations.",
        "It contains structured knowledge — NOT raw conversation history.",
        "",
        f"Current graph: {skill_count} skills, {event_count} events, {task_count} tasks, {edge_count} relationships.",
    ]

    if selected_nodes:
        sections.extend([
            "",
            f"**{len(selected_nodes)} nodes recalled from OTHER conversations** — these are proven solutions that worked before.",
            "Apply them directly when the current situation matches their trigger conditions.",
        ])

    sections.extend([
        "",
        "## What's below has been recalled FOR THIS QUERY",
        "",
        "The following context was retrieved specifically for the user's current message.",
        "It is NOT generic background — it is the most relevant knowledge from your past experience.",
        "",
        "**Three layers, read in order:**",
        "",
        "1. **`<knowledge_graph>`** — Structured triples (TASK/SKILL/EVENT nodes + edges),",
        "   organized by community clusters. Each `<community>` groups related knowledge.",
        "   Edges show causation: SOLVED_BY, USED_SKILL, REQUIRES, PATCHES, CONFLICTS_WITH.",
        "",
        "2. **`gm_search` tool** — If the above doesn't cover the user's question,",
        "   search for more nodes.",
        "",
        "Use `gm_record` to save new discoveries worth remembering for future sessions.",
        "",
        "**When to apply this knowledge:**",
        "- Encountering an error → check EVENT nodes for matching past errors and their SOLVED_BY skills",
        "- Starting a familiar task → check TASK nodes and their USED_SKILL edges for reusable workflows",
    ])

    if is_rich:
        sections.extend([
            "",
            "**Graph navigation:** Edges show how knowledge connects:",
            "- `SOLVED_BY`: an EVENT was fixed by a SKILL — apply the skill when you see similar errors",
            "- `USED_SKILL`: a TASK used a SKILL — reuse the same approach for similar tasks",
            "- `PATCHES`: a newer SKILL corrects an older one — prefer the newer version",
            "- `CONFLICTS_WITH`: two SKILLs are mutually exclusive — check conditions before choosing",
        ])

    return "\n".join(sections)


def assemble_context(
    db: Connection,
    recalled_nodes: list[GmNode],
    recalled_edges: list[GmEdge]
) -> AssembleResult:
    """
    Assemble the knowledge graph into XML context

    Args:
        db: SQLite database connection
        recalled_nodes: List of recalled nodes
        recalled_edges: List of recalled edges

    Returns:
        Result dict with XML, system prompt, and token count
    """

    # Include all recalled nodes without truncation
    node_map: dict[str, GmNode] = {}

    for n in recalled_nodes:
        node_map[n.id] = n

    # Sort: SKILL priority > validated_count > global pagerank
    type_pri: dict[NodeType, int] = {NodeType.SKILL: 3, NodeType.TASK: 2, NodeType.EVENT: 1}

    # Recalled nodes are already PPR-sorted; include them all
    selected = sorted(
        node_map.values(),
        key=lambda n: (
            -(type_pri.get(n.type, 0)),  # SKILL first
            -n.validated_count,  # validated_count descending
            -n.pagerank  # pagerank descending
        )
    )


    if not selected:
        return {
            'xml': None,
            'system_prompt': '',
            'tokens': 0
        }

    # Map node IDs to names
    id_to_name: dict[str, str] = {n.id: n.name for n in selected}
    selected_ids: set[str] = {n.id for n in selected}

    # Filter edges that connect nodes within the selected set
    seen_edges: set[str] = set()
    edges: list[GmEdge] = [
        e for e in recalled_edges
        if e.from_id in selected_ids
           and e.to_id in selected_ids
           and e.id not in seen_edges
           and not seen_edges.add(e.id)  # type: ignore
    ]

    # Group nodes by community
    by_community: dict[str, list[GmNode]] = {}
    no_community: list[GmNode] = []

    for n in selected:
        if n.community_id:
            cid = n.community_id
            if cid not in by_community:
                by_community[cid] = []
            by_community[cid].append(n)
        else:
            no_community.append(n)

    # Generate node XML grouped by community
    xml_parts: list[str] = []

    for cid, members in by_community.items():
        summary = get_community_summary(db, cid)
        label = escape_xml(summary['summary']) if summary else cid

        xml_parts.append(f'  <community id="{cid}" desc="{label}">')

        for n in members:
            tag = n.type.value.lower()
            updated_date = datetime.fromtimestamp(n.updated_at / 1000).isoformat()[:10]
            time_attr = f' updated="{updated_date}"'

            xml_parts.append(
                f'    <{tag} name="{n.name}" desc="{escape_xml(n.description)}"{time_attr}>\n'
                f'{n.content.strip()}\n'
                f'    </{tag}>'
            )

        xml_parts.append(f'  </community>')

    # Nodes without a community go at the top level
    for n in no_community:
        tag = n.type.value.lower()
        updated_date = datetime.fromtimestamp(n.updated_at / 1000).isoformat()[:10]
        time_attr = f' updated="{updated_date}"'

        xml_parts.append(
            f'  <{tag} name="{n.name}" desc="{escape_xml(n.description)}"{time_attr}>\n'
            f'{n.content.strip()}\n'
            f'  </{tag}>'
        )

    nodes_xml = "\n".join(xml_parts)

    # Generate edge XML
    if edges:
        edge_lines = []
        for e in edges:
            from_name = id_to_name.get(e.from_id, e.from_id)
            to_name = id_to_name.get(e.to_id, e.to_id)
            cond_attr = f' when="{escape_xml(e.condition)}"' if getattr(e, "condition", None) else ''

            edge_line = (
                f'    <e type="{e.type}" from="{from_name}" to="{to_name}"{cond_attr}>'
                f'{escape_xml(e.instruction)}</e>'
            )
            edge_lines.append(edge_line)

        edges_xml = "\n  <edges>\n" + "\n".join(edge_lines) + "\n  </edges>"
    else:
        edges_xml = ""

    xml = f"<knowledge_graph>\n{nodes_xml}{edges_xml}\n</knowledge_graph>"

    # Build system prompt
    selected_node_info = [SelectedNode(type = n.type.value) for n in selected]
    system_prompt = build_system_prompt_addition(selected_node_info, len(edges))

    full_content = system_prompt + "\n\n" + xml

    return {
        'xml': xml,
        'system_prompt': system_prompt,
        'tokens': math.ceil(len(full_content) / CHARS_PER_TOKEN)
    }
