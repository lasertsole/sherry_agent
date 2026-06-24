import json
import math
import asyncio
from loguru import logger
from config import SRC_DIR
from .recaller import Recaller
from .extractor import Extractor
from typing import TypedDict, Any
from pub_func import slice_last_turn
from .async_task_queue import async_task_queue
from langchain_core.embeddings import Embeddings
from models import auxiliary_llm, embed_model
from .type import GmConfig, GmNode, ExtractionResult
from langchain_core.messages import BaseMessage, ToolMessage
from .format import sanitize_tool_use_result_pairing, assemble_context
from pub_func.extract_text_from_content import extract_text_from_content
from .graph import (invalidate_graph_cache, detect_communities, summarize_communities, run_maintenance, CommunityResult,
                    MaintenanceResult)
from .store import (get_db, delete_node, save_message, get_unextracted, get_by_session, upsert_node, find_by_id, find_by_name,
                   upsert_edge, delete_extracted, UpsertResult)
from runtime import state_register_db

class RecallResult(TypedDict):
    nodes: list[Any]
    edges: list[Any]

# ── Initialize Core Modules ────────────────────────────────
DEFAULT_CONFIG: GmConfig = GmConfig(
    db_path=f"{SRC_DIR}/store/skill_memory/skill_memory.db",
    compact_turn_count = 6,
    recall_max_nodes = 6,
    recall_max_depth = 2,
    fresh_tail_count = 10,
    dedup_threshold = 0.90,
    pagerank_damping = 0.85,
    pagerank_iterations = 20,
    embedding = embed_model,
    llm = auxiliary_llm
)

db = get_db()
recaller = Recaller(db, DEFAULT_CONFIG)
extractor = Extractor()

# ── Session Runtime State ────────────────────────────────
msg_seq: dict[str, int] = {}

# ─── Get Last Complete User Turn ──────────────────────────
def estimate_msg_tokens(msg: BaseMessage) -> int:
    content = msg.content

    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content) if content is not None else ""

    return math.ceil(len(text) / 3)

TOKEN_MAX = 6000
def _truncate_msg(msg: BaseMessage)-> BaseMessage:
    if not isinstance(msg, ToolMessage):
        return msg

    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        text:str = json.dumps(content) if content is not None else ""
    else:
        text:str = content

    if len(text) <= TOKEN_MAX:
        return msg

    head_len = int(TOKEN_MAX * 0.6)
    tail_len = int(TOKEN_MAX * 0.3)

    truncated_text = (
        f"{text[:head_len]}\n"
        f"...[truncated {len(text) - head_len - tail_len} chars]...\n"
        f"{text[-tail_len:]}"
    )

    return msg.model_copy(deep=True, update={"content": truncated_text})

# ─── Normalize message content so OpenClaw's content.filter() doesn't crash ──
def normalize_message_content(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Normalize message content format

    - If content is a list → fix malformed text blocks
    - If content is a string → wrap into standard content block list
    - If content is None/undefined → empty text block
    """
    result = []

    for msg in messages:
        c = getattr(msg, "content", None)

        # If content is a list → fix malformed block
        if isinstance(c, list):
            fixed = []
            changed = False

            for block in c:
                if block and isinstance(block, dict) and block.get("type") == "text":
                    if "text" not in block:
                        # Missing text attribute, fill with empty string
                        fixed_block = {**block, "text": ""}
                        fixed.append(fixed_block)
                        changed = True
                        continue

                fixed.append(block)

            # If modified, return new object
            if changed:
                if isinstance(msg, dict):
                    new_msg = {**msg, "content": fixed}
                else:
                    new_msg = msg.model_copy(deep=True, update={"content": fixed})
                result.append(new_msg)
            else:
                result.append(msg)
            continue

        # If content is a string → wrap into standard content block list
        if isinstance(c, str):
            if isinstance(msg, dict):
                new_msg = {**msg, "content": [{"type": "text", "text": c}]}
            else:
                new_msg = msg.model_copy(deep=True, update={"content": [{"type": "text", "text": c}]})
            result.append(new_msg)
            continue

        # If content is None/null → empty text block
        if c is None:
            if isinstance(msg, dict):
                new_msg = {**msg, "content": [{"type": "text", "text": ""}]}
            else:
                new_msg = msg.model_copy(deep=True, update = {"content": [{"type": "text", "text": ""}]})
            result.append(new_msg)
            continue

        # Otherwise return as-is
        result.append(msg)

    return result


def ingest_message(session_id: str, message: BaseMessage)-> None:
    """ Save a message to gm_messages (sync, zero LLM)"""
    global msg_seq

    seq = msg_seq.get(session_id)

    if seq is None:
        # First save: read current max turn_index from DB to avoid overlap after restart
        cursor = db.cursor()
        cursor.execute(
            "SELECT MAX(turn_index) as maxTurn FROM gm_messages WHERE session_id=?",
            (session_id,)
        )
        row = cursor.fetchone()
        seq = row[0] if row and row[0] is not None else 0

    seq += 1
    msg_seq[session_id] = seq

    role = getattr(message, 'type', 'unknown') or 'unknown'
    content: str | list[dict[str, Any]] = getattr(message, 'content', '')

    if role == "human": # extract string content
        content:str = extract_text_from_content(content)

    save_message(db, session_id, seq, role, content)


async def run_turn_extract(session_id: str) -> None:
    """Extract current turn's messages directly after each turn"""

    # Fetch unextracted messages (including the one just saved)
    msgs = get_unextracted(db, session_id, 50)
    if not msgs:
        return

    existing = [node.name for node in get_by_session(db, session_id)]
    result: ExtractionResult = await extractor.extract(messages=msgs, existing_names=existing)

    name_to_id: dict[str, str] = {}
    for nc in getattr(result, "nodes", []):
        upsert_result: UpsertResult = upsert_node(
            db = db,
            c = {
                "type": nc.type,
                "name": nc.name,
                "description": nc.description,
                "content": nc.content,
            },
            session_id = session_id
        )
        node = upsert_result["node"]
        name_to_id[node.name] = node.id

        # Async embedding generation, non-blocking
        async_task_queue.add_task(recaller.sync_embed(node))

    for ec in getattr(result, "edges", []):
        from_id = name_to_id.get(ec.from_node)
        if from_id is None:
            found = find_by_id(db, ec.from_node)
            from_id = found.id if found else None

        to_id = name_to_id.get(ec.to_node)
        if to_id is None:
            found = find_by_name(db, ec.to_node)
            to_id = found.id if found else None

        if from_id and to_id:
            upsert_edge(
                db,
                edge_data = {
                    "from_id": from_id,
                    "to_id": to_id,
                    "type": ec.type,
                    "instruction": ec.instruction,
                    "condition": ec.condition,
                    "session_id": session_id,
                }
            )

    max_turn = max(msg["turn_index"] for msg in msgs)
    delete_extracted(db, session_id, max_turn)

    if getattr(result, "nodes", None) or getattr(result, "edges", None):
        invalidate_graph_cache()

# Assemble system prompt
async def assemble(
        user_text: str,
        messages: list[BaseMessage] | None = None,
) -> dict:
    if not user_text:
        return {
            "messages": normalize_message_content(messages),
            "estimated_tokens": 0
        }

    rec: RecallResult = await recaller.recall(user_text)

    if messages is None:
        messages = []

    if len(rec["nodes"]) == 0:
        return {
            "messages": normalize_message_content(messages),
            "estimated_tokens": 0
        }

    # ── 1. Last Complete Turn ──────────────────────────
    last_turn = slice_last_turn(messages)
    repaired = sanitize_tool_use_result_pairing(last_turn["messages"])

    # ── 2. Graph + Traceability ───────────────────────
    assemble_result = assemble_context(
        db,
        recalled_nodes= rec["nodes"],
        recalled_edges= rec["edges"]
    )
    xml = assemble_result["xml"]
    system_prompt = assemble_result["system_prompt"]
    gm_tokens = assemble_result["tokens"]

    # ── 3. Assemble systemPrompt ─────────────────────
    system_prompt_addition: str | None = None
    parts = [system_prompt, xml]
    filtered_parts = [p for p in parts if p]
    if filtered_parts:
        system_prompt_addition = "\n\n".join(filtered_parts)

    result: dict[str, Any] = {
        "messages": normalize_message_content(repaired),
        "estimated_tokens": gm_tokens + last_turn["tokens"],
    }
    if system_prompt_addition:
        result["system_prompt_addition"] = system_prompt_addition
    return result

async def after_turn(
        session_id: str,
        last_turn_messages: list[BaseMessage],
) -> None:
    """Post-turn processing hook"""
    for message in last_turn_messages:
        ingest_message(session_id, message)

    # ★ Extract every turn (background task)
    async def run_extract():
        await run_turn_extract(session_id)

    asyncio.create_task(run_extract())

    # ★ Community maintenance: triggered every N turns (pure computation, <5ms)
    turns:int = state_register_db.get_state(session_id, "skill_memory_maintain_turns", 0) + 1
    maintain_interval:int = getattr(DEFAULT_CONFIG, 'compact_turn_count', 6)

    if turns >= maintain_interval:
        state_register_db.set_state(session_id, "skill_memory_maintain_turns", 0)

        try:
            invalidate_graph_cache()
            comm: CommunityResult = detect_communities(db)

            # Generate summaries immediately after each community detection (needs LLM), ensuring generalized recall is available
            if comm["communities"] and len(comm["communities"]) > 0:
                embed: Embeddings = getattr(recaller, 'embed', None)
                summaries = await summarize_communities(
                    db,
                    comm["communities"],
                    DEFAULT_CONFIG.llm,
                    embed
                )
                logger.info(
                    f"[skill_memory] community summaries refreshed: "
                    f"{summaries} summaries"
                )

        except Exception as e:
            logger.error(f"[skill_memory] periodic maintenance failed: {e}")
    else:
        state_register_db.set_state(session_id, "skill_memory_maintain_turns", turns)

async def dispose() -> None:
    """Release all memory"""
    msg_seq.clear()


async def rectification_and_standardization(session_id: str) -> None:
    """Session end cleanup and knowledge consolidation"""
    try:
        # Fetch all nodes in this session
        nodes: list[GmNode] = get_by_session(db, session_id)

        if nodes:
            # Get global Top 20 nodes as graph summary
            cursor = db.cursor()
            cursor.execute(
                "SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20"
            )
            top_nodes: list[dict[str, Any]] = [dict(r) for r in cursor.fetchall()]

            # Build summary string
            summary_parts: list[str] = []
            for n in top_nodes:
                name = n['name']
                node_type = n['type']
                validated_count = n['validated_count']
                pagerank = n['pagerank']
                summary_parts.append(
                    f"{node_type}:{name}(v{validated_count},pr{pagerank})"
                )
            summary: str = ", ".join(summary_parts)

            # Call finalizer for end-of-session review
            fin = await extractor.finalize(
                session_nodes=nodes,
                graph_summary=summary
            )

            # Handle promoted skills
            for nc in fin.promoted_skills:
                if nc.name and nc.content:
                    upsert_node(
                        db,
                        {
                            "type": "SKILL",
                            "name": nc.name,
                            "description": nc.description or "",
                            "content": nc.content,
                        },
                        session_id
                    )

            # Handle new edges
            for ec in fin.new_edges:
                from_node = find_by_name(db, ec.from_node)
                to_node = find_by_name(db, ec.to_node)

                if from_node and to_node:
                    upsert_edge(
                        db,
                        {
                            "from_id": from_node.id,
                            "to_id": to_node.id,
                            "type": ec.type,
                            "instruction": ec.instruction,
                            "session_id": session_id,
                        }
                    )

            # Mark invalid nodes
            for node_id in fin.invalidations:
                delete_node(db, node_id)

        # Execute graph maintenance
        embed: Embeddings | None = getattr(recaller, "embed", None)
        result: MaintenanceResult = await run_maintenance(db, DEFAULT_CONFIG, DEFAULT_CONFIG.llm, embed)

        # Record maintenance log
        top_pr_names = [
            f"{n['name']}({n['score']})"
            for n in result["pagerank"]["top_k"][:3]
        ]

        logger.info(
            f"[skill_memory] maintenance: {result['duration_ms']}ms, "
            f"dedup={result['dedup']['merged']}, "
            f"communities={result['community']['count']}, "
            f"summaries={result['community_summaries']}, "
            f"top_pr={', '.join(top_pr_names)}"
        )

    except Exception as e:
        logger.error(f"[skill_memory] session_end error: {e}")
    finally:
        # Clean up session state
        msg_seq.pop(session_id, None)