import logging
import textwrap
import numpy as np
from .store import PersistentGraph
from pydantic import BaseModel, Field
from models.chat_model import chat_model
from typing import List, Tuple, Dict, Set
from models.embed_model.core import embed_model

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger("MultiHopGraphRAG")

def multi_hop_search(
        graph: PersistentGraph,
        query: str,
        llm,
        max_hops: int = 3,
        top_k_entry: int = 3,
        use_hybrid: bool = True,
) -> Dict:
    """
    1. Embed query -> find entry nodes via hybrid search (embedding + FTS5).
    2. From each entry node, BFS through edges up to max_hops.
       At each hop, LLM judges which neighbours are relevant.
    3. Collect all visited passages and summarise via LLM.
    """
    # ---- Step 1: entry nodes ----
    q_emb = np.array(embed_model.embed_query(query))
    entry_nodes = graph.search_entry_nodes(
        query, q_emb, top_k=top_k_entry, use_hybrid=use_hybrid
    )
    logger.info("Entry nodes: %s", [(nid, f"{score:.3f}") for nid, score in entry_nodes])

    # ---- Step 2: BFS ----
    visited: Set[int] = set()
    queue: List[Tuple[int, int, str]] = []  # (node_id, hop, path_desc)
    all_paths: List[Dict] = []

    for nid, _ in entry_nodes:
        queue.append((nid, 0, f"[entry] node_{nid}"))
        visited.add(nid)

    while queue:
        current_id, hop, path_desc = queue.pop(0)
        if hop >= max_hops:
            continue

        out_edges = graph.get_out_edges(current_id)
        candidates = [
            (e.target_id, graph.node_text(e.target_id), e.bridge_relation)
            for e in out_edges if e.target_id not in visited
        ]
        if not candidates:
            continue

        # LLM judges relevance among candidates
        cand_str = "\n".join(
            f"[{i}] (rel: {rel}) {txt[:200]}"
            for i, (_, txt, rel) in enumerate(candidates)
        )

        class RelevanceJudgeOutput(BaseModel):
            relevant_indices: List[int] = Field(
                description="与查询相关的文档片段的索引列表。如果没有相关的，返回空列表 []。",
                default=[]
            )

        structured_judge = chat_model.with_structured_output(RelevanceJudgeOutput, method="json_mode")

        result: RelevanceJudgeOutput = structured_judge.invoke(textwrap.dedent(f"""\
        Query: {query}

        We are traversing a knowledge graph step by step.
        We are CURRENTLY at this passage (node_{current_id}):
        {graph.node_text(current_id)[:200]}

        From here, we can follow edges to these connected passages:

        {cand_str}

        We need to continue the multi-hop traversal. The goal is to find passages
        that help answer the query, even indirectly — bridging concepts count.

        CRITICAL RULES:
        - If a candidate mentions related people, concepts, technologies, or topics → SELECT IT
        - If multiple candidates are related → SELECT ALL of them
        - ALWAYS select at least the most promising candidate if any are even tangentially related
        - Only return an empty list if absolutely NOTHING is related

        Output the result in JSON format. The JSON must have a key "relevant_indices" containing a list of integers.
        Examples: 
        - If nodes 0 and 2 are related: {{"relevant_indices": [0, 2]}}
        - If no nodes are related: {{"relevant_indices": []}}
        """))
        selected: list[int] = result.relevant_indices

        if not selected or len(selected) == 0:
            continue

        for idx in selected:
            if idx >= len(candidates):
                continue
            tid, txt, rel = candidates[idx]
            visited.add(tid)
            all_paths.append({
                "hop": hop + 1,
                "node_id": tid,
                "text": txt,
                "relation": rel,
                "path": path_desc + f" -> [{hop + 1}] (via {rel}) node_{tid}",
            })
            queue.append((tid, hop + 1, path_desc + f" -> [{hop + 1}] node_{tid}"))

    # ---- Step 3: summarise ----
    if not all_paths and not entry_nodes:
        return {"answer": "No relevant information found.", "paths": [], "visited": len(visited)}

    context_parts = []
    for nid, score in entry_nodes:
        context_parts.append(f"[Entry node_{nid}] {graph.node_text(nid)}")
    for p in all_paths:
        context_parts.append(f"[Hop {p['hop']} via '{p['relation']}'] {p['text']}")

    context = "\n\n".join(context_parts)

    summary = llm.invoke(textwrap.dedent(f"""\
    Based on the multi-hop search results below, answer the query.

    Query: {query}

    Search context (multi-hop graph traversal):
    {context}

    Provide a concise answer. If insufficient info, say so. Trace the multi-hop path.
    """))
    answer = summary.content if hasattr(summary, "content") else str(summary)

    return {
        "answer": answer.strip(),
        "entry_nodes": [(nid, score) for nid, score in entry_nodes],
        "paths": all_paths,
        "visited": len(visited),
    }