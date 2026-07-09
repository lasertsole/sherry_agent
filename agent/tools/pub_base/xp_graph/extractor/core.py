"""
xp_graph — Knowledge Graph Extraction Engine
"""

import json
from ..type import GmNode
from typing import Literal
from models import main_llm
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import SystemMessage, HumanMessage
from pub_func import sanitize_content, escape_xml, escape_prompt_braces

# ─── Node/Edge Valid Values ───────────────────────────────────────
VALID_NODE_TYPES = {"TASK", "SKILL", "EVENT"}
VALID_EDGE_TYPES = {"USED_SKILL", "SOLVED_BY", "REQUIRES", "PATCHES", "CONFLICTS_WITH"}

# Edge type → valid from node types
EDGE_FROM_CONSTRAINT: dict[str, set[str]] = {
    "USED_SKILL": {"TASK"},
    "SOLVED_BY": {"EVENT", "SKILL"},
    "REQUIRES": {"SKILL"},
    "PATCHES": {"SKILL"},
    "CONFLICTS_WITH": {"SKILL"},
}

# Edge type → valid to node types
EDGE_TO_CONSTRAINT: dict[str, set[str]] = {
    "USED_SKILL": {"SKILL"},
    "SOLVED_BY": {"SKILL"},
    "REQUIRES": {"SKILL"},
    "PATCHES": {"SKILL"},
    "CONFLICTS_WITH": {"SKILL"},
}


# ─── Type Definitions ─────────────────────────────────────────────
class Node(BaseModel):
    """A knowledge graph node"""
    type: Literal["TASK", "SKILL", "EVENT"] = Field(description="Node type")
    name: str = Field(description="Node name")
    description: str = Field(description="Node description")
    content: str = Field(description="Node content")


class Edge(BaseModel):
    """A knowledge graph edge"""
    from_node: str = Field(description="Edge source node name")
    to_node: str = Field(description="Edge target node name")
    type: str = Field(description="Edge type")
    instruction: str = Field(description="Edge execution instruction")
    condition: str | None = Field(default=None, description="Edge trigger condition")


class ExtractionResult(BaseModel):
    """Extraction result containing nodes and edges"""
    nodes: list[Node] = Field(description="Extracted node list", default=[])
    edges: list[Edge] = Field(description="Extracted edge list", default=[])


class PromotedSkill(Node):
    """A skill promoted from an EVENT node"""
    type: Literal["SKILL"]


class FinalizeResult(BaseModel):
    """Finalization result containing promoted skills, new edges, and invalidations"""
    promoted_skills: list[PromotedSkill]
    new_edges: list[Edge]
    invalidations: list[str]


# ─── Extraction System Prompt ─────────────────────────────────────

EXTRACT_SYS = escape_prompt_braces("""You are the xp_graph knowledge graph extraction engine. Extract reusable structured knowledge triples (nodes + edges) from AI Agent conversations.
Extracted knowledge will be recalled in future conversations, helping the Agent avoid repeating mistakes and reuse proven solutions.
Output strict JSON: {"nodes":[...],"edges":[...]}, with no extra text.

1. Node Extraction:
   1.1 Identify three types of knowledge nodes from the conversation:
       - TASK: A specific task the user asked the Agent to complete, or a topic discussed, analyzed, or compared in the conversation
       - SKILL: A reusable operational skill with specific tools/commands/APIs, clear trigger conditions, and directly executable steps
       - EVENT: A one-time error or exception, recording the symptom, cause, and solution
   1.2 Every node must include all 4 fields:
       - type: Node type, only TASK / SKILL / EVENT allowed
       - name: Lowercase hyphenated name, ensure consistent naming across the entire extraction
       - description: One sentence describing what scenario triggers this
       - content: Knowledge content in plain text format
   1.3 name naming convention:
       - TASK: verb-object format, e.g., deploy-bilibili-mcp, extract-pdf-tables, compare-ocr-engines
       - SKILL: tool-action format, e.g., conda-env-create, docker-port-expose
       - EVENT: phenomenon-tool format, e.g., importerror-libgl1, timeout-paddleocr
       - An existing node list will be provided; reuse existing names for the same entity — do not create duplicate nodes
   1.4 content templates (plain text, choose by type):
       TASK → "[name]\nObjective: ...\nSteps:\n1. ...\n2. ...\nResult: ..."
       SKILL → "[name]\nTrigger: ...\nSteps:\n1. ...\n2. ...\nCommon Errors:\n- ... -> ..."
       EVENT → "[name]\nSymptom: ...\nCause: ...\nSolution: ..."

2. Edge Extraction:
   2.1 Identify direct, explicit relationships between nodes. Only the following 5 edge types are allowed.
   2.2 Every edge must include from_node, to_node, type, instruction — all 4 fields required.
   2.3 Edge type definitions and direction constraints (strictly follow, do not mix):

       USED_SKILL
         Direction: TASK → SKILL (and only this direction)
         Meaning: The task used this skill during execution
         instruction: Which step used it, how it was called, what parameters were passed
         Condition: from_node is TASK, to_node is SKILL

       SOLVED_BY
         Direction: EVENT → SKILL or SKILL → SKILL
         Meaning: The error/problem was resolved by this skill
         instruction: What specific command/operation was executed to resolve it
         condition (required): What error or condition triggered this solution
         Condition: from_node is EVENT or SKILL, to_node is SKILL
         Note: TASK nodes cannot be the from_node of SOLVED_BY. Tasks using skills must use USED_SKILL.

       REQUIRES
         Direction: SKILL → SKILL
         Meaning: This skill requires another skill to be completed first
         instruction: Why the dependency exists, how to determine if the prerequisite is met

       PATCHES
         Direction: SKILL → SKILL (new → old)
         Meaning: A new skill corrects/replaces an old approach
         instruction: What was wrong with the old solution, what the new approach changed

       CONFLICTS_WITH
         Direction: SKILL ↔ SKILL (bidirectional)
         Meaning: Two skills are mutually exclusive in the same scenario
         instruction: Specific conflict symptoms, which one to choose

   2.4 Relationship direction decision tree (evaluate in this order):
       a. from_node is TASK, to_node is SKILL → must use USED_SKILL
       b. from_node is EVENT, to_node is SKILL → must use SOLVED_BY
       c. from_node and to_node are both SKILL → choose SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH based on semantics
       d. No other valid combinations exist. Do not extract relationships that don't match any of the above.

3. Extraction Strategy (better to over-extract than miss):
   3.1 Attempt to extract from all conversation content, including discussions, analyses, comparisons, technology selection, etc.
   3.2 When the user corrects an AI error, extract both the old and new approaches, linked with a PATCHES edge.
   3.3 Extracting discussions and comparisons as TASK nodes, recording conclusions and key points.
   3.4 Only pure greetings (e.g., "hello", "thanks") should be skipped.

4. Output Specification:
   4.1 Return only JSON, format: {"nodes":[...],"edges":[...]}
   4.2 No markdown code block wrapping, no explanatory text, no extra fields
   4.3 When no knowledge is produced, return {"nodes":[],"edges":[]}
   4.4 Each edge's instruction must contain specific executable content — cannot be empty or say "see above"

Example 1 (TASK + SKILL + USED_SKILL edge):

Conversation summary: The user asked to scrape Bilibili danmaku. The Agent used the bili-tool danmaku subcommand.

Output:
{"nodes":[{"type":"TASK","name":"extract-bilibili-danmaku","description":"Batch scrape danmaku data from Bilibili videos","content":"extract-bilibili-danmaku\nObjective: Scrape all danmaku from a specific Bilibili video\nSteps:\n1. Get the video BV ID\n2. Call bili-tool danmaku --bv BVxxx\n3. Output JSON-formatted danmaku list\nResult: Successfully scraped 2341 danmaku entries"},{"type":"SKILL","name":"bili-tool-danmaku","description":"Use bili-tool to scrape Bilibili video danmaku","content":"bili-tool-danmaku\nTrigger: When needing to scrape Bilibili video danmaku\nSteps:\n1. pip install bilibili-api-python\n2. python bili_tool.py danmaku --bv BVxxx --output danmaku.json\nCommon Errors:\n- cookie expired -> re-fetch SESSDATA"}],"edges":[{"from_node":"extract-bilibili-danmaku","to_node":"bili-tool-danmaku","type":"USED_SKILL","instruction":"Step 2 calls bili-tool danmaku subcommand with --bv and --output parameters"}]}

Example 2 (EVENT + SKILL + SOLVED_BY edge):

Conversation summary: Running PaddleOCR reported libGL missing, resolved by apt install.

Output:
{"nodes":[{"type":"EVENT","name":"importerror-libgl1","description":"libGL.so.1 missing when importing cv2/paddleocr","content":"importerror-libgl1\nSymptom: ImportError: libGL.so.1: cannot open shared object file\nCause: OpenCV depends on system-level libGL library, not auto-installed by conda/pip\nSolution: apt install -y libgl1-mesa-glx"},{"type":"SKILL","name":"apt-install-libgl1","description":"Install libgl1 to fix missing OpenCV system dependency","content":"apt-install-libgl1\nTrigger: ImportError: libGL.so.1\nSteps:\n1. sudo apt update\n2. sudo apt install -y libgl1-mesa-glx\nCommon Errors:\n- Permission denied -> add sudo"}],"edges":[{"from_node":"importerror-libgl1","to_node":"apt-install-libgl1","type":"SOLVED_BY","instruction":"Execute sudo apt install -y libgl1-mesa-glx","condition":"When ImportError: libGL.so.1 is reported"}]}

Return empty arrays for nothing to process. Return only JSON, no extra text.
""")

# ─── Extraction User Prompt ───────────────────────────────────────
def extract_user_prompt(msgs: str, existing: str) -> str:
    """Build the extraction user prompt"""
    return f"""<Existing Nodes>
{existing or "(none)"}

<Conversation>
{msgs}"""


# ─── Finalization System Prompt ───────────────────────────────────
FINALIZE_SYS = """You are the graph node finalization engine. Perform a final review of nodes generated in this session before it ends.
Review all nodes from this session and execute the following three operations. Output strict JSON.

1. Promote EVENT to SKILL:
    If an EVENT node has general reusable value (not limited to a specific scenario), promote it to SKILL.
    When promoting: rename to SKILL naming convention (tool-action), update content to SKILL plain text template format.
    Write to promotedSkills array.

2. Add Missing Edges:
    Review all nodes holistically to find cross-node relationships that were hard to detect during single extraction.
    Edge types allowed: USED_SKILL, SOLVED_BY, REQUIRES, PATCHES, CONFLICTS_WITH.
    Strictly follow direction constraints: TASK->SKILL use USED_SKILL, EVENT->SKILL use SOLVED_BY.
    Write to newEdges array.

3. Mark Obsolete Nodes:
    Old nodes invalidated by new discoveries in this session — write their node_id to the invalidations array.

Return empty arrays for nothing to process. Return only JSON, no extra text.
Format: {"promoted_skills":[{"type":"SKILL","name":"...","description":"...","content":"..."}],"new_edges":[{"from_node":"...","to_node":"...","type":"...","instruction":"...","condition":"..."}],"invalidations":["node-id"]}"""

# ─── Finalization User Prompt ─────────────────────────────────────
def finalize_user_prompt(nodes: list[GmNode], summary: str) -> str:
    """Build the finalization user prompt"""
    nodes_summary = json.dumps([
        {
            'id': n.id,
            'type': n.type.value if hasattr(n.type, 'value') else str(n.type),
            'name': n.name,
            'description': n.description,
            'v': getattr(n, 'validated_count', 0)
        }
        for n in nodes
    ], indent=2, ensure_ascii=False)

    return f"""<Session Nodes>
    
{nodes_summary}

<Graph Summary>
{summary}"""

# ─── Extractor ────────────────────────────────────────────────
class Extractor:
    """Knowledge graph extractor"""
    @staticmethod
    async def extract(messages: list[dict], existing_names: list[str]) -> ExtractionResult:
        """
        Extract a knowledge graph from conversation messages

        Args:
            messages: List of conversation messages
            existing_names: List of existing node names

        Returns:
            Extraction result containing nodes and edges
        """
        # Format messages
        msgs_parts = []
        for m in messages:
            role = m.get('role', '?').upper()
            turn_index = m.get('turn_index', 0)
            content = m.get('content', '')

            if isinstance(content, str):
                text = content
            else:
                text = json.dumps(content, ensure_ascii=False)

            # Filter special characters
            text = sanitize_content(text)
            text = escape_xml(text)

            msg_text = f"[{role} t={turn_index}]\n{text[:800]}"
            msgs_parts.append(msg_text)

        msgs = "\n\n---\n\n".join(msgs_parts)

        structured_llm = ChatPromptTemplate.from_messages([
            ("system", EXTRACT_SYS),  # EXTRACT_SYS is the extraction system prompt
            ("human", "{user_input}")
        ]) | main_llm.with_structured_output(ExtractionResult)

        return structured_llm.invoke({"user_input": extract_user_prompt(msgs, ", ".join(existing_names))})

    @staticmethod
    async def finalize(session_nodes: list[GmNode], graph_summary: str) -> FinalizeResult:
        """
        Final review before session end

        Args:
            session_nodes: List of nodes in this session
            graph_summary: Graph summary

        Returns:
            Result containing promoted skills, new edges, and invalidations
        """
        return main_llm.with_structured_output(FinalizeResult, method='json_mode').invoke(
            [SystemMessage(FINALIZE_SYS), HumanMessage(finalize_user_prompt(session_nodes, graph_summary))],
            max_tokens=16384
        )
