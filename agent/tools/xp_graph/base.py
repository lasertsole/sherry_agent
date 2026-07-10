import json
import math
from typing import Any
from loguru import logger
from config import SRC_DIR
from .recaller import Recaller
from .extractor import Extractor
from .type import GmConfig, GmNode
from pub_func import slice_last_turn
from runtime import state_register_db
from .async_task_queue import async_task_queue
from langchain_core.embeddings import Embeddings
from models import auxiliary_llm, embed_model
from langchain_core.messages import BaseMessage, ToolMessage
from .format import sanitize_tool_use_result_pairing, assemble_context
from .store import get_db, delete_node, get_by_session, upsert_node, find_by_name, upsert_edge, UpsertResult
from .graph import (invalidate_graph_cache, detect_communities, summarize_communities, run_maintenance, CommunityResult,
                    MaintenanceResult)


class XpGraphInstance:
    """A self-contained xp_graph instance bound to a specific DB role.

    Each instance holds its own db connection, recaller, extractor, and config.
    Roles: "default" (main agent + commander, strategy-level), "worker" (operation-level).
    """

    def __init__(self, role: str = "default"):
        self.role = role
        self.db = get_db(role)
        self.config = GmConfig(
            db_path=f"{SRC_DIR}/store/xp_graph/{role}/xp_graph.db" if role != "default"
                     else f"{SRC_DIR}/store/xp_graph/xp_graph.db",
            compact_turn_count=7,
            recall_max_nodes=6,
            recall_max_depth=2,
            fresh_tail_count=10,
            dedup_threshold=0.90,
            pagerank_damping=0.85,
            pagerank_iterations=20,
            embedding=embed_model,
            llm=auxiliary_llm,
        )
        self.recaller = Recaller(self.db, self.config)
        self.extractor = Extractor()

    async def assemble(self, user_text: str, messages: list[BaseMessage] | None = None) -> dict:
        if not user_text:
            return {"messages": normalize_message_content(messages), "estimated_tokens": 0}

        rec = await self.recaller.recall(user_text)

        if messages is None:
            messages = []

        if len(rec["nodes"]) == 0:
            return {"messages": normalize_message_content(messages), "estimated_tokens": 0}

        last_turn = slice_last_turn(messages)
        repaired = sanitize_tool_use_result_pairing(last_turn["messages"])

        assemble_result = assemble_context(
            self.db,
            recalled_nodes=rec["nodes"],
            recalled_edges=rec["edges"],
        )
        xml = assemble_result["xml"]
        system_prompt = assemble_result["system_prompt"]
        gm_tokens = assemble_result["tokens"]

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

    async def ingest_experiences(self, session_id: str, experiences: list) -> None:
        name_to_id: dict[str, str] = {}
        for exp in experiences:
            exp_type = exp.type if isinstance(exp.type, str) else exp.type.value
            upsert_result: UpsertResult = upsert_node(
                db=self.db,
                c={
                    "type": exp_type,
                    "name": exp.name,
                    "description": exp.description,
                    "content": exp.content,
                },
                session_id=session_id,
            )
            node = upsert_result["node"]
            name_to_id[node.name] = node.id
            async_task_queue.add_task(self.recaller.sync_embed(node))

        if name_to_id:
            invalidate_graph_cache()

        logger.debug(f"[xp_graph:{self.role}] ingested {len(experiences)} experiences")

    async def after_turn(self, session_id: str, last_turn_experiences: list | None = None) -> None:
        if last_turn_experiences:
            await self.ingest_experiences(session_id, last_turn_experiences)

        xp_graph_maintain_turns: int = state_register_db.get_state(session_id, "xp_graph_maintain_turns", 0) + 1
        maintain_interval: int = getattr(self.config, 'compact_turn_count', 7)
        logger.debug(f"[xp_graph:{self.role}] maintain turn {xp_graph_maintain_turns} / {maintain_interval}")

        if xp_graph_maintain_turns >= maintain_interval:
            state_register_db.set_state(session_id, "xp_graph_maintain_turns", 0)
            try:
                invalidate_graph_cache()
                comm: CommunityResult = detect_communities(self.db)
                if comm["communities"] and len(comm["communities"]) > 0:
                    embed: Embeddings = getattr(self.recaller, 'embed', None)
                    await summarize_communities(self.db, comm["communities"], self.config.llm, embed)
            except Exception as e:
                logger.error(f"[xp_graph:{self.role}] periodic maintenance failed: {e}")
        else:
            state_register_db.set_state(session_id, "xp_graph_maintain_turns", xp_graph_maintain_turns)

        xp_graph_rect_turns: int = state_register_db.get_state(session_id, "xp_graph_rectification_and_standardization_turns", 0) + 1
        if xp_graph_rect_turns % 13 == 0:
            state_register_db.set_state(session_id, "xp_graph_rectification_and_standardization_turns", 0)
            await self.rectification_and_standardization(session_id)
        else:
            state_register_db.set_state(session_id, "xp_graph_rectification_and_standardization_turns", xp_graph_rect_turns)

    async def rectification_and_standardization(self, session_id: str) -> None:
        try:
            nodes: list[GmNode] = get_by_session(self.db, session_id)
            if nodes:
                cursor = self.db.cursor()
                cursor.execute("SELECT name, type, validated_count, pagerank FROM gm_nodes ORDER BY pagerank DESC LIMIT 20")
                top_nodes: list[dict[str, Any]] = [dict(r) for r in cursor.fetchall()]

                summary_parts: list[str] = []
                for n in top_nodes:
                    summary_parts.append(f"{n['type']}:{n['name']}(v{n['validated_count']},pr{n['pagerank']})")
                summary: str = ", ".join(summary_parts)

                fin = await self.extractor.finalize(session_nodes=nodes, graph_summary=summary)

                for nc in fin.promoted_skills:
                    if nc.name and nc.content:
                        upsert_node(self.db, {"type": "SKILL", "name": nc.name, "description": nc.description or "", "content": nc.content}, session_id)

                for ec in fin.new_edges:
                    from_node = find_by_name(self.db, ec.from_node)
                    to_node = find_by_name(self.db, ec.to_node)
                    if from_node and to_node:
                        upsert_edge(self.db, {"from_id": from_node.id, "to_id": to_node.id, "type": ec.type, "instruction": ec.instruction, "session_id": session_id})

                for node_id in fin.invalidations:
                    delete_node(self.db, node_id)

            embed: Embeddings | None = getattr(self.recaller, "embed", None)
            result: MaintenanceResult = await run_maintenance(self.db, self.config, self.config.llm, embed)

            top_pr_names = [f"{n['name']}({n['score']})" for n in result["pagerank"]["top_k"][:3]]
            logger.debug(
                f"[xp_graph:{self.role}] maintenance: {result['duration_ms']}ms, "
                f"dedup={result['dedup']['merged']}, communities={result['community']['count']}, "
                f"summaries={result['community_summaries']}, top_pr={', '.join(top_pr_names)}"
            )
        except Exception as e:
            logger.error(f"[xp_graph:{self.role}] session_end error: {e}")


# ── Module-level convenience: default instance (backward compatible) ──

_default_instance = XpGraphInstance(role="default")

db = _default_instance.db
recaller = _default_instance.recaller
extractor = _default_instance.extractor
DEFAULT_CONFIG = _default_instance.config

_instances: dict[str, XpGraphInstance] = {"default": _default_instance}


def get_instance(role: str = "default") -> XpGraphInstance:
    """Get or create a XpGraphInstance for the given role."""
    if role not in _instances:
        _instances[role] = XpGraphInstance(role=role)
    return _instances[role]


# ── Backward-compatible module-level functions ──

async def assemble(user_text: str, messages: list[BaseMessage] | None = None) -> dict:
    return await _default_instance.assemble(user_text, messages)


async def _ingest_experiences(session_id: str, experiences: list) -> None:
    await _default_instance.ingest_experiences(session_id, experiences)


async def after_turn(session_id: str, last_turn_experiences: list | None = None) -> None:
    await _default_instance.after_turn(session_id, last_turn_experiences)


async def rectification_and_standardization(session_id: str) -> None:
    await _default_instance.rectification_and_standardization(session_id)


async def dispose() -> None:
    pass


# ── Utility functions ──

def estimate_msg_tokens(msg: BaseMessage) -> int:
    content = msg.content
    if isinstance(content, str):
        text = content
    else:
        text = json.dumps(content) if content is not None else ""
    return math.ceil(len(text) / 3)

TOKEN_MAX = 6000

def _truncate_msg(msg: BaseMessage) -> BaseMessage:
    if not isinstance(msg, ToolMessage):
        return msg
    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        text: str = json.dumps(content) if content is not None else ""
    else:
        text: str = content
    if len(text) <= TOKEN_MAX:
        return msg
    head_len = int(TOKEN_MAX * 0.6)
    tail_len = int(TOKEN_MAX * 0.3)
    truncated_text = f"{text[:head_len]}\n...[truncated {len(text) - head_len - tail_len} chars]...\n{text[-tail_len:]}"
    return msg.model_copy(deep=True, update={"content": truncated_text})


def normalize_message_content(messages: list[BaseMessage]) -> list[BaseMessage]:
    result = []
    for msg in messages:
        c = getattr(msg, "content", None)
        if isinstance(c, list):
            fixed = []
            changed = False
            for block in c:
                if block and isinstance(block, dict) and block.get("type") == "text":
                    if "text" not in block:
                        fixed.append({**block, "text": ""})
                        changed = True
                        continue
                fixed.append(block)
            if changed:
                if isinstance(msg, dict):
                    new_msg = {**msg, "content": fixed}
                else:
                    new_msg = msg.model_copy(deep=True, update={"content": fixed})
                result.append(new_msg)
            else:
                result.append(msg)
            continue
        if isinstance(c, str):
            if isinstance(msg, dict):
                new_msg = {**msg, "content": [{"type": "text", "text": c}]}
            else:
                new_msg = msg.model_copy(deep=True, update={"content": [{"type": "text", "text": c}]})
            result.append(new_msg)
            continue
        if c is None:
            if isinstance(msg, dict):
                new_msg = {**msg, "content": [{"type": "text", "text": ""}]}
            else:
                new_msg = msg.model_copy(deep=True, update={"content": [{"type": "text", "text": ""}]})
            result.append(new_msg)
            continue
        result.append(msg)
    return result
