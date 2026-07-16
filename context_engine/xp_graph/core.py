import json
import textwrap
from loguru import logger
from .recaller import Recaller
from .format import assemble_context
from pydantic import BaseModel, Field
from .store.db import get_db, resolve_db_path
from .async_task_queue import async_task_queue
from models.LLMs.main_llm import build_main_llm
from langchain_core.tools import tool, BaseTool
from langchain_core.embeddings import Embeddings
from typing import Literal, Any, TypedDict, Annotated
from .type import  ExtractionResult, GmNode, GmConfig
from langgraph.prebuilt.tool_node import InjectedState
from runtime import state_register_mem, state_register_db
from models import build_embed_model, build_auxiliary_llm
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.messages import SystemMessage, HumanMessage
from . import CommunityResult, detect_communities, summarize_communities
from .graph import MaintenanceResult, run_maintenance, invalidate_graph_cache
from .store import delete_node, get_by_session, upsert_node, find_by_name, upsert_edge, UpsertResult, sync_node_embed


def build_config(role: str)-> GmConfig:
    return GmConfig(
        db_path=resolve_db_path(role).as_posix(),
        compact_turn_count=7,
        recall_max_nodes=6,
        recall_max_depth=2,
        fresh_tail_count=10,
        dedup_threshold=0.90,
        pagerank_damping=0.85,
        pagerank_iterations=20,
        embedding=build_embed_model(),
        llm=build_auxiliary_llm(),
    )

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


class Node(BaseModel):
    """A knowledge graph node"""
    type: Literal["TASK", "SKILL", "EVENT"] = Field(description="Node type")
    name: str = Field(description="Node name")
    description: str = Field(description="Node description")
    content: str = Field(description="Node content")


class PromotedSkill(Node):
    """A skill promoted from an EVENT node"""
    type: Literal["SKILL"]

class Edge(BaseModel):
    """A knowledge graph edge"""
    from_node: str = Field(description="Edge source node name")
    to_node: str = Field(description="Edge target node name")
    type: str = Field(description="Edge type")
    instruction: str = Field(description="Edge execution instruction")
    condition: str | None = Field(default=None, description="Edge trigger condition")

class FinalizeResult(BaseModel):
    """Finalization result containing promoted skills, new edges, and invalidations"""
    promoted_skills: list[PromotedSkill]
    new_edges: list[Edge]
    invalidations: list[str]


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

    return textwrap.dedent(f"""\
        <Session Nodes>

        {nodes_summary}

        <Graph Summary>
        {summary}
    """)


async def finalize(session_nodes: list[GmNode], graph_summary: str) -> FinalizeResult:
    """
    Final review before session end

    Args:
        session_nodes: List of nodes in this session
        graph_summary: Graph summary

    Returns:
        Result containing promoted skills, new edges, and invalidations
    """
    model = build_main_llm()
    return model.with_structured_output(FinalizeResult, method='json_mode').invoke(
        [SystemMessage(FINALIZE_SYS), HumanMessage(finalize_user_prompt(session_nodes, graph_summary))],
        max_tokens=16384
    )

async def _rectification_and_standardization(session_id: str, role: str) -> None:
    try:
        db = get_db(role)
        nodes: list[GmNode] = get_by_session(db, session_id)
        recaller = Recaller(db, build_config(role))
        if nodes:
            cursor = db.cursor()
            cursor.execute(
                "SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20")
            top_nodes: list[dict[str, Any]] = [dict(r) for r in cursor.fetchall()]

            summary_parts: list[str] = []
            for n in top_nodes:
                summary_parts.append(f"{n['type']}:{n['name']}(v{n['validated_count']},pr{n['pagerank']})")
            summary: str = ", ".join(summary_parts)

            fin = await finalize(session_nodes=nodes, graph_summary=summary)

            for nc in fin.promoted_skills:
                if nc.name and nc.content:
                    upsert_node(db, {"type": "SKILL", "name": nc.name, "description": nc.description or "",
                                          "content": nc.content}, session_id)

            for ec in fin.new_edges:
                from_node = find_by_name(db, ec.from_node)
                to_node = find_by_name(db, ec.to_node)
                if from_node and to_node:
                    upsert_edge(db, {"from_id": from_node.id, "to_id": to_node.id, "type": ec.type,
                                          "instruction": ec.instruction, "session_id": session_id})

            for node_id in fin.invalidations:
                delete_node(db, node_id)

        embed: Embeddings | None = getattr(recaller, "embed", None)
        llm = build_auxiliary_llm()
        result: MaintenanceResult = await run_maintenance(db, build_config(role), llm, embed)

        top_pr_names = [f"{n['name']}({n['score']})" for n in result["pagerank"]["top_k"][:3]]
        logger.debug(
            f"[xp_graph:{role}] maintenance: {result['duration_ms']}ms, "
            f"dedup={result['dedup']['merged']}, communities={result['community']['count']}, "
            f"summaries={result['community_summaries']}, top_pr={', '.join(top_pr_names)}"
        )
    except Exception as e:
        logger.error(f"[xp_graph:{role}] session_end error: {e}")

# ─── ExperienceTrace Models ─────────────────────────────────────
class Fix(BaseModel):
    """A fix strategy for a failure."""
    strategy: Literal["parameter", "approach", "workaround"] = Field(
        description="Type of fix strategy"
    )
    description: str = Field(description="Description of the fix")
    tool: str | None = Field(default=None, description="Tool used for the fix")


class Failure(BaseModel):
    """A failure encountered during task execution."""
    symptom: str = Field(description="What went wrong (symptom)")
    cause: str = Field(description="Root cause of the failure")
    fixes: list[Fix] = Field(default=[], description="Fixes applied or attempted")

class PathStep(BaseModel):
    """A step in the execution path."""
    tool: str = Field(description="Tool or skill used")
    input: str = Field(description="Input or command executed")
    output: str | None = Field(default=None, description="Output or result")
    trigger: str | None = Field(default=None, description="Why this tool/approach was chosen over alternatives")


class ExperienceTrace(BaseModel):
    """Structured trace of an agent's experience during task execution.
    
    This is the core data structure for the draft system. It captures:
    - The task being performed
    - The execution path (tools used, inputs, outputs)
    - Failures encountered and fixes applied
    - Dependencies on other skills
    """
    task: str = Field(description="Description of the task being performed")
    path: list[PathStep] = Field(default=[], description="Execution path steps")
    failures: list[Failure] = Field(default=[], description="Failures encountered")
    requires: list[str] | None = Field(default=None, description="Required skills or prerequisites")

_XP_GRAPH_DRAFT: str = "xp_graph_draft"

class RecallResult(TypedDict):
    nodes: list[Any]
    edges: list[Any]

def update_draft(session_id: str, system_prompt: str, messages: list[BaseModel]) -> None:
    raw_state: ExperienceTrace | None = state_register_mem.get_state(session_id, "xp_graph_draft", None)
    if raw_state is None:
        additional_prompt: str = ""
    else:
        # Already an ExperienceTrace object — serialize it back to text
        additional_prompt = _serialize_draft(raw_state)

    if additional_prompt.strip() != "":
        additional_prompt = f"Already extracted structured experience data:\n{additional_prompt}\nModify the existing structured experience based on actual conditions\n\n"

    distill_prompt: str = (
        "Review the conversation messages above and extract structured ExperienceTrace data.\n"
        f"{additional_prompt}"
        "Output strictly in JSON with the following fields:\n\n"
        "1. task: Core objective of this task (one sentence summary).\n"
        "2. path: Execution path, each step contains:\n"
        "   - tool: Tool or skill name used\n"
        "   - input: Input/command executed or content\n"
        "   - output: Output result (if any)\n"
        "   - trigger: Reason for choosing this tool/approach (if any)\n"
        "3. failures: Failure records encountered, each contains:\n"
        "   - symptom: Failure symptom\n"
        "   - cause: Root cause\n"
        "   - fixes: List of fix strategies attempted (strategy is one of parameter/approach/workaround, description describes the fix, tool is optional)\n"
        "4. requires: List of prerequisite skills or conditions needed for this task\n\n"
        "If there is no corresponding content, path is [], failures is [], requires is null."
        "Output only JSON, no extra explanation."
    )
    parser = PydanticOutputParser(pydantic_object=ExperienceTrace)
    json_mode_llm = build_main_llm()
    json_mode_llm = json_mode_llm.bind(response_format={"type": "json_object"})
    raw_basemodel = json_mode_llm.invoke(
        input=[SystemMessage(content=system_prompt), *messages, HumanMessage(content=distill_prompt)]
    )
    experience_trace: ExperienceTrace = parser.parse(raw_basemodel.content)
    logger.debug("update_draft experience_trace is {}", experience_trace)

    state_register_mem.set_state(session_id, _XP_GRAPH_DRAFT, experience_trace)

def _serialize_draft(draft: ExperienceTrace) -> str:
    """Serialize ExperienceTrace into a structured text for LLM consumption."""
    parts = [f"Task: {draft.task}"]

    if draft.path:
        parts.append("\nExecution Path:")
        for i, step in enumerate(draft.path, 1):
            parts.append(f"  Step {i}:")
            parts.append(f"    Tool: {step.tool}")
            parts.append(f"    Input: {step.input}")
            if step.output:
                parts.append(f"    Output: {step.output}")
            if step.trigger:
                parts.append(f"    Reason: {step.trigger}")

    if draft.failures:
        parts.append("\nFailures:")
        for i, fail in enumerate(draft.failures, 1):
            parts.append(f"  Failure {i}:")
            parts.append(f"    Symptom: {fail.symptom}")
            parts.append(f"    Cause: {fail.cause}")
            for j, fix in enumerate(fail.fixes, 1):
                parts.append(f"    Fix {j}:")
                parts.append(f"      Strategy: {fix.strategy}")
                parts.append(f"      Description: {fix.description}")
                if fix.tool:
                    parts.append(f"      Tool: {fix.tool}")

    if draft.requires:
        parts.append("\nPrerequisites:")
        for req in draft.requires:
            parts.append(f"  - {req}")

    return "\n".join(parts)


EXTRACT_GRAPH_SYS = """You are the xp_graph knowledge graph extraction engine. Extract reusable structured knowledge (nodes + edges) from a structured experience trace.

Output strict JSON: {{"nodes":[...],"edges":[...]}}, with no extra text.

The input is a structured experience trace containing:
- Task: What the agent was asked to do
- Execution Path: Steps taken (tools, inputs, outputs, reasons)
- Failures: Errors encountered (symptom, cause, fixes)
- Prerequisites: Required skills or dependencies

1. Node Extraction:
   1.1 Three node types:
       - TASK: The task the agent performed
       - SKILL: A reusable operational skill with specific tools/commands
       - EVENT: A failure or error, recording symptom, cause, and solution
   1.2 Every node must include all 4 fields:
       - type: TASK / SKILL / EVENT
       - name: Lowercase hyphenated name, consistent across extraction
       - description: One sentence describing what scenario triggers this
       - content: Knowledge content in plain text format
   1.3 name naming convention:
       - TASK: verb-object format, e.g., deploy-bilibili-mcp
       - SKILL: tool-action format, e.g., conda-env-create, docker-port-expose
       - EVENT: phenomenon-tool format, e.g., importerror-libgl1
   1.4 content templates (plain text, choose by type):
       TASK → "[name]\\nObjective: ...\\nSteps:\\n1. ...\\n2. ...\\nResult: ..."
       SKILL → "[name]\\nTrigger: ...\\nSteps:\\n1. ...\\n2. ...\\nCommon Errors:\\n- ... -> ..."
       EVENT → "[name]\\nSymptom: ...\\nCause: ...\\nSolution: ..."

2. Edge Extraction:
   2.1 Only 5 edge types allowed. Every edge must include from_node, to_node, type, instruction.
   2.2 Edge types and direction constraints:

       USED_SKILL (TASK → SKILL only)
         instruction: Which step used it, how it was called, what parameters were passed
         condition: Why this skill was chosen (from Reason field)

       SOLVED_BY (EVENT → SKILL or SKILL → SKILL)
         instruction: What was executed to resolve the issue
         condition (required): The error symptom that triggered this solution

       REQUIRES (SKILL → SKILL)
         instruction: Why the dependency exists, how to determine if prerequisite is met

       PATCHES (SKILL → SKILL, new → old)
         instruction: What was wrong with the old approach, what the new one changed

       CONFLICTS_WITH (SKILL ↔ SKILL, bidirectional)
         instruction: Specific conflict symptoms, which one to choose

3. Extraction Strategy:
   3.1 Extract the task as a single TASK node.
   3.2 Each distinct tool/approach in the execution path should be a SKILL node.
   3.3 Each failure should be an EVENT node. Fix with a tool → SKILL + SOLVED_BY edge.
   3.4 Prerequisites → SKILL nodes + REQUIRES edges.
   3.5 Look for relationships BETWEEN path steps: if step N replaces/corrects step N-1's approach, add a PATCHES edge. If two tools conflict, add CONFLICTS_WITH.
   3.6 Also look for relationships BETWEEN skills derived from path AND skills derived from fix tools — if a fix tool corrects an approach used in a path step, add PATCHES.
   3.7 content should be rich and actionable — use the full input/output/reason details from the trace.

4. Output Specification:
    4.1 Return only JSON: {{"nodes":[...],"edges":[...]}}
   4.2 No markdown code block wrapping, no explanatory text
   4.3 Each edge's instruction must contain specific executable content"""


XP_GRAPH_MAINTAIN_TURNS: str = "xp_graph_maintain_turns"
MAINTAIN_INTERVAL: int = 7
RECTIFICATION_AND_STANDARDIZATION_TURNS: str = "xp_graph_rectification_and_standardization_turns"
RECTIFICATION_AND_STANDARDIZATION_INTERVAL: int = 13

async def _maybe_maintain_communities(
    db,
    session_id: str,
    role: str,
) -> None:
    """Run community detection + summarization every MAINTAIN_INTERVAL turns.

    This is a periodic maintenance task triggered by turn count stored in state_register_db.
    """
    xp_graph_maintain_turns: int = state_register_db.get_state(session_id, XP_GRAPH_MAINTAIN_TURNS, 0) + 1
    logger.debug(f"[skill_memory] summarize_communities turn {xp_graph_maintain_turns} / {MAINTAIN_INTERVAL}")
    if xp_graph_maintain_turns >= MAINTAIN_INTERVAL:
        state_register_db.set_state(session_id, XP_GRAPH_MAINTAIN_TURNS, 0)

        try:
            invalidate_graph_cache()
            comm: CommunityResult = detect_communities(db)

            # Generate summaries immediately after each community detection (needs LLM), ensuring generalized recall is available
            if comm["communities"] and len(comm["communities"]) > 0:
                recaller = Recaller(db, build_config(role))
                embed: Embeddings = getattr(recaller, 'embed', None)
                llm = build_auxiliary_llm()
                summaries = await summarize_communities(
                    db,
                    comm["communities"],
                    llm,
                    embed
                )
                logger.info(
                    f"[skill_memory] community summaries refreshed: "
                    f"{summaries} summaries"
                )

        except Exception as e:
            logger.error(f"[skill_memory] periodic maintenance failed: {e}")
    else:
        state_register_db.set_state(session_id, XP_GRAPH_MAINTAIN_TURNS, xp_graph_maintain_turns)


async def _maybe_run_rectification(session_id: str, role: str) -> None:
    """Run rectification_and_standardization every RECTIFICATION_AND_STANDARDIZATION_INTERVAL turns.

    This is a periodic maintenance task triggered by turn count stored in state_register_db.
    """
    xp_graph_rectification_and_standardization_turns: int = (
        state_register_db.get_state(session_id, RECTIFICATION_AND_STANDARDIZATION_TURNS, 0) + 1
    )
    logger.info(
        f"[skill_memory] rectification_and_standardization turn "
        f"{xp_graph_rectification_and_standardization_turns} / {RECTIFICATION_AND_STANDARDIZATION_INTERVAL}"
    )
    if xp_graph_rectification_and_standardization_turns % RECTIFICATION_AND_STANDARDIZATION_INTERVAL == 0:
        state_register_db.set_state(session_id, RECTIFICATION_AND_STANDARDIZATION_TURNS, 0)
        await _rectification_and_standardization(session_id=session_id, role=role)
    else:
        state_register_db.set_state(session_id, RECTIFICATION_AND_STANDARDIZATION_TURNS,
                                    xp_graph_rectification_and_standardization_turns)


async def extract(
    session_id: str,
    role: Literal["default", "commander", "worker"],
) -> None:
    """
    Convert the ExperienceTrace draft stored in session state into an
    ExtractionResult (knowledge graph nodes + edges) using LLM reasoning,
    then persist the result to the database.

    The LLM receives the full ExperienceTrace as structured text and
    autonomously decides the optimal node/edge representation, including
    implicit relationships (PATCHES, CONFLICTS_WITH) that the old
    mechanical mapping could not capture.

    The database connection is resolved from ``role`` via ``get_db(role)``.

    Persistence steps: node dedup + upsert, edge upsert, async
    embedding, cache invalidation.

    Args:
        session_id: Session identifier.
        role: Role name (e.g. "default", "commander", "worker").

    Returns:
        ExtractionResult containing extracted nodes and edges.

    Raises:
        RuntimeError: If no draft exists for the given session_id.
    """
    draft: ExperienceTrace | None = state_register_mem.get_state(session_id, _XP_GRAPH_DRAFT, None)
    if draft is None:
        raise RuntimeError("xp draft is None")

    from langchain_core.prompts import ChatPromptTemplate

    serialized = _serialize_draft(draft)
    model = build_main_llm()
    structured_llm = ChatPromptTemplate.from_messages([
        ("system", EXTRACT_GRAPH_SYS),
        ("human", "Extract nodes and edges from the following experience trace:\n\n{trace}"),
    ]) | model.with_structured_output(ExtractionResult)

    result: ExtractionResult = structured_llm.invoke({"trace": serialized})

    # ── Resolve db from role ──
    db = get_db(role)

    # ── Persist extracted nodes/edges ──
    from loguru import logger

    # ── Node dedup & upsert ──
    name_to_id: dict[str, str] = {}
    for node in result.nodes:
        existing = find_by_name(db, node.name)
        if existing is not None:
            name_to_id[node.name] = existing.id
            continue

        node_type = node.type if isinstance(node.type, str) else node.type.value
        upsert_result: UpsertResult = upsert_node(
            db=db,
            c={
                "type": node_type,
                "name": node.name,
                "description": node.description,
                "content": node.content,
            },
            session_id=session_id,
            )
        gm_node = upsert_result["node"]
        if gm_node is not None:
            name_to_id[node.name] = gm_node.id
            embed_model = build_embed_model()
            async_task_queue.add_task(sync_node_embed(db, gm_node, embed_model))

    # ── Edge upsert ──
    for edge in result.edges:
        from_node = find_by_name(db, edge.from_id)
        to_node = find_by_name(db, edge.to_id)
        if from_node is None or to_node is None:
            logger.warning(
                f"[xp_graph:{role}] skipping edge {edge.from_id}→{edge.to_id}: "
                f"node not found in DB"
            )
            continue
        upsert_edge(
            db=db,
            edge_data={
                "from_id": from_node.id,
                "to_id": to_node.id,
                "type": edge.type.value,
                "instruction": edge.instruction,
                "condition": edge.condition,
                "session_id": session_id,
            },
        )

    if name_to_id:
        invalidate_graph_cache()

    logger.debug(f"[xp_graph:{role}] persisted extraction: {len(result.nodes)} nodes, {len(result.edges)} edges")

    await _maybe_maintain_communities(db, session_id, role)

    await _maybe_run_rectification(session_id, role)


class XPRetrieveSchema(BaseModel):
    query: str = Field(description="Query string used to search the experience knowledge graph for relevant methods, skills, or error solutions")

@tool("xp_retrieve", args_schema=XPRetrieveSchema)
async def xp_retrieve_tool(query: str, role: Annotated[str, InjectedState("session_id")] = "")-> str:
    """Search the experience knowledge graph for relevant methods, then summarize them into a readable answer.

    Recalls similar historical tasks, reusable skills, and past error solutions
    from the experience knowledge graph, then filters and organizes the results
    into a concise answer. Returns "No relevant methods found." when nothing
    matches the query."""
    db = get_db(role)

    recaller = Recaller(db, build_config(role))
    rec: RecallResult = await recaller.recall(query)
    nodes = rec["nodes"]
    edges = rec["edges"]

    if not nodes:
        return "No relevant methods found."

    assemble_result = assemble_context(
        db,
        recalled_nodes=nodes,
        recalled_edges=edges
    )
    xml = assemble_result["xml"]

    llm = build_auxiliary_llm(temperature=0.0)
    filter_prompt = (
        "You are an experience knowledge graph query assistant. Below is the recalled node and edge data (XML format).\n\n"
        "Graph structure:\n"
        "- Three node types: SKILL (reusable operational method), TASK (historical task), EVENT (historical error and solution)\n"
        "- Nodes are grouped by community; knowledge within the same community is related\n"
        "- Edge types:\n"
        "  · USED_SKILL: A TASK used a SKILL\n"
        "  · SOLVED_BY: An EVENT was resolved by a SKILL\n"
        "  · REQUIRES: One SKILL depends on another SKILL\n"
        "  · PATCHES: A newer SKILL corrects an older one\n"
        "  · CONFLICTS_WITH: Two SKILLs are mutually exclusive\n\n"
        "Filter the XML data below based on the user's question and return only the relevant methods.\n"
        f"User question: {query}\n\n"
        "Recalled knowledge graph data:\n"
        f"{xml}\n\n"
        "Rules:\n"
        "1. Keep only nodes and edges directly relevant to the user's question\n"
        "2. If relevant content is found, present it in clear, concise language\n"
        "3. If no relevant method is found, reply with exactly: No relevant methods found.\n"
        "4. Do not fabricate information — base your answer solely on the recalled data"
    )
    try:
        response = await llm.ainvoke(filter_prompt)
        if hasattr(response, 'content'):
            raw = response.content
            result = raw[0] if isinstance(raw, list) else raw
        else:
            result = str(response)
        result = str(result).strip()
        if "No relevant methods found." in result:
            return "No relevant methods found."
        return result
    except Exception as e:
        logger.error(f"[xp_retrieve] LLM filtering failed: {e}")
        return xml or "No relevant methods found."

def build_xp_retrieve_tool()-> BaseTool:
    xp_retrieve_tool.handle_tool_error = True
    xp_retrieve_tool.metadata = {"idempotent": True}
    return xp_retrieve_tool