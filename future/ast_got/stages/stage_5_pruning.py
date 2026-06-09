import datetime
from loguru import logger
from typing import Dict, Any, List, Set

from models import simple_chat_model
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage
from future.ast_got.models.graph import AGoTGraph
from tests import calculate_semantic_overlap


# ============================================================
# Pydantic 模型：AI 结构化输出
# ============================================================

class ThresholdSuggestion(BaseModel):
    """AI 对剪枝/合并阈值的动态建议"""
    pruning_threshold: float = Field(description="Suggested pruning confidence threshold (0-1)")
    impact_threshold: float = Field(description="Suggested pruning impact threshold (0-1)")
    merging_threshold: float = Field(description="Suggested merging overlap threshold (0-1)")
    reasoning: str = Field(description="Why these thresholds were chosen for this graph")


class PruningDecision(BaseModel):
    """AI 对单个节点是否剪枝的判断"""
    should_prune: bool = Field(description="Whether this node should be pruned")
    reasoning: str = Field(description="Scientific explanation for the pruning decision")


class MergeJudgment(BaseModel):
    """AI 对两个节点是否应该合并的判断"""
    should_merge: bool = Field(description="Whether these two nodes represent the same/overlapping concept")
    reasoning: str = Field(description="Why these nodes should or should not be merged")


class NodeFusion(BaseModel):
    """AI 生成的合并后节点综合内容"""
    merged_label: str = Field(description="Concise merged label covering both nodes (≤80 chars)")
    merged_description: str = Field(description="Synthesized description merging both nodes' core content")


class PruningStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Pruning/Merging Stage")

        parameters = context.get("parameters", {})
        query = context.get("query", "")

        pruning_threshold = parameters.get("pruning_threshold", 0.2)
        impact_threshold = parameters.get("impact_threshold", 0.3)
        merging_threshold = parameters.get("merging_threshold", 0.8)

        # --- [AI] 动态阈值优化 ---
        refined = self._ai_refine_thresholds(
            graph, query, pruning_threshold, impact_threshold, merging_threshold
        )
        if refined:
            pruning_threshold, impact_threshold, merging_threshold = refined
            logger.info(
                f"AI refined thresholds: pruning={pruning_threshold:.3f}, "
                f"impact={impact_threshold:.3f}, merge={merging_threshold:.3f}"
            )

        pruned_nodes = 0
        merged_nodes = 0

        # ===== Phase 1: Pruning =====
        nodes_to_prune: Set[str] = set()
        borderline_nodes: List[tuple] = []  # 需要 AI 判断的"灰色地带"节点

        for node_id, node_data in graph.graph.nodes(data=True):
            if node_data.get("node_type") in ["root", "dimension"]:
                continue

            confidence = node_data.get("confidence", [])
            if not confidence:
                continue

            min_confidence = min(confidence)
            impact_score = node_data.get("impact_score", 0.5)

            # 明显低于阈值 → 规则直接剪枝（快速通道）
            if min_confidence < pruning_threshold * 0.7 and impact_score < impact_threshold * 0.7:
                nodes_to_prune.add(node_id)
                logger.debug(
                    f"Pruned node {node_id} (rule): confidence={min_confidence:.3f}, impact={impact_score:.3f}"
                )
            # "灰色地带" → 交给 AI 判断
            elif min_confidence < pruning_threshold and impact_score < impact_threshold:
                borderline_nodes.append((node_id, node_data, min_confidence, impact_score))

        # --- [AI] 灰色地带节点剪枝判断 ---
        if borderline_nodes:
            ai_pruned = self._ai_pruning_decisions(graph, query, borderline_nodes)
            for node_id in ai_pruned:
                nodes_to_prune.add(node_id)

        # ===== Phase 2: Merge Candidate Detection =====
        nodes_to_merge: Dict[str, str] = {}

        node_types: Dict[str, list] = {}
        for node_id, node_data in graph.graph.nodes(data=True):
            if node_id in nodes_to_prune:
                continue
            node_type = node_data.get("node_type")
            if node_type not in node_types:
                node_types[node_type] = []
            node_types[node_type].append(node_id)

        for node_type, nodes in node_types.items():
            if len(nodes) <= 1:
                continue

            for i, node1_id in enumerate(nodes):
                if node1_id in nodes_to_prune or node1_id in nodes_to_merge:
                    continue

                for node2_id in nodes[i + 1:]:
                    if node2_id in nodes_to_prune or node2_id in nodes_to_merge:
                        continue

                    node1_data = graph.graph.nodes[node1_id]
                    node2_data = graph.graph.nodes[node2_id]

                    overlap = calculate_semantic_overlap(node1_data, node2_data)

                    if overlap >= merging_threshold:
                        # 高度重叠 → 规则决定保留哪个（快速通道）
                        conf1 = sum(node1_data.get("confidence", [0.5])) / max(
                            len(node1_data.get("confidence", [0.5])), 1
                        )
                        conf2 = sum(node2_data.get("confidence", [0.5])) / max(
                            len(node2_data.get("confidence", [0.5])), 1
                        )
                        impact1 = node1_data.get("impact_score", 0.5)
                        impact2 = node2_data.get("impact_score", 0.5)
                        score1 = conf1 * impact1
                        score2 = conf2 * impact2

                        if score1 >= score2:
                            nodes_to_merge[node2_id] = node1_id
                        else:
                            nodes_to_merge[node1_id] = node2_id

                        logger.debug(
                            f"Marked nodes for merging (rule): {node1_id} <- {node2_id} | overlap={overlap:.3f}"
                        )

                    elif overlap >= merging_threshold * 0.75:
                        # 中等重叠 → [AI] 判断是否合并
                        should_merge, suggested_keep = self._ai_merge_judgment(
                            graph, node1_id, node2_id
                        )
                        if should_merge:
                            keep_id = suggested_keep if suggested_keep in (node1_id, node2_id) else node1_id
                            victim = node2_id if keep_id == node1_id else node1_id
                            nodes_to_merge[victim] = keep_id
                            logger.debug(
                                f"Marked nodes for merging (AI): {keep_id} <- {victim} | overlap={overlap:.3f}"
                            )

        # ===== Phase 3: Execute Pruning =====
        for node_id in nodes_to_prune:
            if node_id in graph.graph:
                graph.graph.remove_node(node_id)
                pruned_nodes += 1

        # ===== Phase 4: Execute Merging =====
        for source_id, target_id in nodes_to_merge.items():
            if source_id not in graph.graph or target_id not in graph.graph:
                continue

            # 转移边关系
            for u, v, data in graph.graph.edges(source_id, data=True):
                graph.graph.add_edge(target_id, v, **data)
            for u, v, data in graph.graph.in_edges(source_id, data=True):
                graph.graph.add_edge(u, target_id, **data)

            source_data = graph.graph.nodes[source_id]
            target_data = graph.graph.nodes[target_id]

            # --- [AI] 内容融合（含兜底逻辑） ---
            self._ai_fuse_nodes(source_data, target_data)

            # 记录合并历史
            if "revision_history" not in target_data:
                target_data["revision_history"] = []
            target_data["revision_history"].append({
                "timestamp": str(datetime.datetime.now()),
                "action": "merge",
                "source_node": source_id,
                "description": f"Merged with node {source_id}"
            })

            graph.graph.remove_node(source_id)
            merged_nodes += 1

        logger.info(
            f"Pruning/Merging complete: Pruned {pruned_nodes} nodes, merged {merged_nodes} nodes"
        )

        return {
            "summary": (
                f"Simplified graph by pruning {pruned_nodes} low-confidence nodes "
                f"and merging {merged_nodes} similar nodes"
            ),
            "metrics": {
                "pruned_nodes": pruned_nodes,
                "merged_nodes": merged_nodes,
                "remaining_nodes": graph.graph.number_of_nodes(),
                "remaining_edges": graph.graph.number_of_edges()
            }
        }

    # ============================================================
    # AI 辅助方法
    # ============================================================

    def _ai_refine_thresholds(
        self, graph: AGoTGraph, query: str,
        pruning_t: float, impact_t: float, merge_t: float
    ):
        """[AI] 根据图谱特性和查询动态优化剪枝/合并阈值

        当 AI 调用失败时返回 None，回退到原始阈值。
        """
        try:
            node_count = graph.graph.number_of_nodes()
            edge_count = graph.graph.number_of_edges()

            all_tags = []
            for _, data in graph.graph.nodes(data=True):
                tags = data.get("disciplinary_tags", [])
                if isinstance(tags, list):
                    all_tags.extend(tags)
            unique_tags = len(set(all_tags))

            prompt = HumanMessage(content=f"""
Analyze this knowledge graph and suggest optimal pruning/merging thresholds.

Query: {query}
Graph State: {node_count} nodes, {edge_count} edges, {unique_tags} unique disciplines.

Current defaults:
- Prune if min(confidence) < {pruning_t:.2f} AND impact < {impact_t:.2f}
- Merge if semantic overlap >= {merge_t:.2f}

Adjustment guidelines:
- Larger graphs need more aggressive pruning (lower thresholds)
- More disciplines need higher merging thresholds to prevent false merges
- If query is complex, be more conservative with pruning

Return adjusted thresholds (0-1) and your reasoning.
""")

            result = simple_chat_model.with_structured_output(ThresholdSuggestion).invoke([prompt])
            if isinstance(result, ThresholdSuggestion):
                return (result.pruning_threshold, result.impact_threshold, result.merging_threshold)
        except Exception as e:
            logger.warning(f"AI threshold refinement failed: {e}")
        return None

    def _ai_pruning_decisions(
        self, graph: AGoTGraph, query: str,
        borderline_nodes: List[tuple]
    ) -> Set[str]:
        """[AI] 判断灰色地带节点是否应该被剪枝

        分批调用 AI，避免 Prompt 过长。
        当 AI 调用失败时，保守保留所有灰色地带节点。
        """
        pruned: Set[str] = set()
        try:
            batch_size = 5
            for batch_start in range(0, len(borderline_nodes), batch_size):
                batch = borderline_nodes[batch_start:batch_start + batch_size]

                descriptions = []
                for node_id, nd, conf, imp in batch:
                    label = nd.get("label", "Unknown")[:80]
                    node_type = nd.get("node_type", "unknown")
                    tags = nd.get("disciplinary_tags", [])
                    desc = nd.get("metadata", {}).get("description", "")[:150]
                    descriptions.append(
                        f"- ID: {node_id} | Type: {node_type} | Label: {label}\n"
                        f"  Confidence: {conf:.3f} | Impact: {imp:.3f} | Tags: {tags}\n"
                        f"  Description: {desc}"
                    )

                prompt = HumanMessage(content=f"""
You are a knowledge graph curator. Decide whether these "gray area" nodes should be pruned.

Research Query: {query}

Nodes:
{chr(10).join(descriptions)}

For each node:
- PRUNE: if it contributes little unique value
- KEEP: if it contains non-redundant insight critical to the query

Consider the node's content, uniqueness, and relevance to the research question.
""")

                result = simple_chat_model.with_structured_output(List[PruningDecision]).invoke([prompt])

                if isinstance(result, list):
                    for i, d in enumerate(result):
                        if isinstance(d, PruningDecision) and i < len(batch):
                            node_id = batch[i][0]
                            if d.should_prune:
                                pruned.add(node_id)
                                logger.info(f"AI pruned {node_id}: {d.reasoning}")
                            else:
                                logger.debug(f"AI kept {node_id}: {d.reasoning}")

        except Exception as e:
            logger.warning(f"AI pruning decisions failed, keeping borderline nodes: {e}")
        return pruned

    def _ai_merge_judgment(
        self, graph: AGoTGraph, n1_id: str, n2_id: str
    ) -> tuple:
        """[AI] 判断两个中等相似度节点是否应合并

        返回 (should_merge, suggested_keep_id)
        当 AI 调用失败时返回 (False, n1_id)
        """
        try:
            d1 = graph.graph.nodes[n1_id]
            d2 = graph.graph.nodes[n2_id]

            def safe_get(data: Dict, key: str, default="") -> str:
                val = data.get(key, default)
                if isinstance(val, str):
                    return val[:200]
                return str(val)[:200]

            prompt = HumanMessage(content=f"""
Determine if these two knowledge graph nodes represent the same concept.

Node1 ({n1_id}):
  Label: {safe_get(d1, 'label')}
  Desc: {str(d1.get('metadata', {}).get('description', ''))[:200]}
  Tags: {d1.get('disciplinary_tags', [])}
  Confidence: {d1.get('confidence', [])}  Impact: {d1.get('impact_score', 'N/A')}

Node2 ({n2_id}):
  Label: {safe_get(d2, 'label')}
  Desc: {str(d2.get('metadata', {}).get('description', ''))[:200]}
  Tags: {d2.get('disciplinary_tags', [])}
  Confidence: {d2.get('confidence', [])}  Impact: {d2.get('impact_score', 'N/A')}

A valid merge requires:
1. They describe the same or highly overlapping concept
2. Merging reduces redundancy without losing important distinctions
3. They belong to compatible disciplines

Return should_merge and reasoning.
""")

            result = simple_chat_model.with_structured_output(MergeJudgment).invoke([prompt])
            if isinstance(result, MergeJudgment) and result.should_merge:
                # 由规则决定保留哪个节点（更可靠的评分机制）
                conf1 = sum(d1.get("confidence", [0.5])) / max(len(d1.get("confidence", [0.5])), 1)
                conf2 = sum(d2.get("confidence", [0.5])) / max(len(d2.get("confidence", [0.5])), 1)
                imp1 = d1.get("impact_score", 0.5)
                imp2 = d2.get("impact_score", 0.5)
                keep = n1_id if (conf1 * imp1) >= (conf2 * imp2) else n2_id
                logger.info(f"AI merge: {n1_id} <-> {n2_id}, keeping {keep}: {result.reasoning}")
                return True, keep

        except Exception as e:
            logger.warning(f"AI merge judgment failed: {e}")
        return False, n1_id

    def _ai_fuse_nodes(self, src: Dict, tgt: Dict):
        """[AI] 合并节点内容：基础字段融合 + AI 综合摘要

        先执行字段级别的兜底合并，
        再尝试用 AI 生成综合的标签和描述。
        """
        # 基础字段融合（兜底逻辑，保证数据完整性）
        if "disciplinary_tags" in src and "disciplinary_tags" in tgt:
            tgt["disciplinary_tags"] = list(set(
                tgt["disciplinary_tags"] + src["disciplinary_tags"]
            ))

        if "bias_flags" in src:
            tgt.setdefault("bias_flags", [])
            if isinstance(src["bias_flags"], list):
                tgt["bias_flags"].extend(src["bias_flags"])

        # [AI] 生成综合标签和描述
        try:
            def safe_val(data: Dict, key: str, alt_key: str = "") -> str:
                val = data.get(key, "")
                if not val and alt_key:
                    val = data.get(alt_key, "")
                if isinstance(val, str):
                    return val[:200]
                return str(val)[:200]

            src_desc = safe_val(src.get("metadata", {}), "description", "")
            tgt_desc = safe_val(tgt.get("metadata", {}), "description", "")
            tgt_label = tgt.get("label", "")
            src_label = src.get("label", "")

            prompt = HumanMessage(content=f"""
Consolidate two knowledge graph nodes into one. Generate a unified label and description.

Target Node (keep):
  Label: "{tgt_label}"
  Description: {tgt_desc}

Source Node (merge in):
  Label: "{src_label}"
  Description: {src_desc}

Requirements:
- merged_label: Concise (≤80 chars), covering essence of both nodes
- merged_description: Synthesized content preserving all unique insights
""")

            result = simple_chat_model.with_structured_output(NodeFusion).invoke([prompt])

            if isinstance(result, NodeFusion):
                if result.merged_label and len(result.merged_label) <= 150:
                    tgt["label"] = result.merged_label
                if result.merged_description:
                    metadata = tgt.setdefault("metadata", {})
                    metadata["description"] = result.merged_description[:1000]
                logger.info(f"AI fused label: [{src.get('label')}] + [{tgt.get('label')}]")

        except Exception as e:
            logger.warning(f"AI content fusion failed, using basic merge: {e}")