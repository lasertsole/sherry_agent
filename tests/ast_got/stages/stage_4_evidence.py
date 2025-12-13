import json
import random
import logging
import datetime
from typing import Dict, Any, List
from models import simple_chat_model
from langchain.agents import create_agent
from tests.ast_got.models.node import Node
from tests.ast_got.models.edge import Edge
from langchain_core.messages import HumanMessage
from tests.ast_got.models.graph import AGoTGraph
from tests.ast_got.models.hyperedge import Hyperedge
from tools import build_web_search_tool, build_python_repl_tool
from tests.ast_got.utils.math_utils import bayesian_update, calculate_info_gain
from pydantic import BaseModel, Field

logger = logging.getLogger("agot-stage4")

class EdgeTypeOutput(BaseModel):
    type: str = Field(description="Relationship type: 'supportive', 'contradictory', 'correlative', 'causal', or 'temporal'")
    subtype: str | None = Field(default=None, description="Subtype if applicable (e.g., 'direct', 'indirect', 'precedence')")
    causal_metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata for causal relationships (e.g., confounders)")
    temporal_metadata: Dict[str, Any] = Field(default_factory=dict, description="Metadata for temporal relationships (e.g., delay, pattern)")

class IBNDecision(BaseModel):
    should_create: bool = Field(description="Whether an interdisciplinary bridge is needed")
    reasoning: str = Field(description="Explanation of the interdisciplinary connection or lack thereof")
    bridge_strength: float = Field(description="Strength of the interdisciplinary link (0.0-1.0)")

class BiasFlag(BaseModel):
    node_id: str = Field(description="The ID of the node where bias was detected")
    flag_type: str = Field(description="Type of bias: 'confirmation_bias', 'sampling_bias', 'anchoring_bias', or 'selection_bias'")
    description: str = Field(description="Detailed explanation of the detected bias")
    severity: str = Field(description="Severity level: 'low', 'medium', or 'high'")

class TopologyAction(BaseModel):
    action: str = Field(description="Action to take: 'merge', 'prune', 'reweight', or 'create_link'")
    target_node_id: str = Field(description="The ID of the node this action applies to")
    reasoning: str = Field(description="Scientific justification for this topological change")
    confidence_threshold: float = Field(default=0.0, description="If pruning, the threshold used")

class TemporalPattern(BaseModel):
    edge_id: str = Field(description="The ID of the edge connecting the evidence to the hypothesis")
    pattern_type: str = Field(description="Type of temporal relationship: 'immediate', 'delayed', 'cumulative', or 'cyclic'")
    delay_description: str = Field(description="Description of the time lag (e.g., '2-4 weeks', 'after 3 cycles')")
    trend: str = Field(description="Observed trend over time: 'increasing', 'decreasing', 'stable', or 'fluctuating'")

class DecayFactor(BaseModel):
    node_id: str = Field(description="The ID of the node to apply decay to")
    decay_rate: float = Field(description="The rate at which confidence should decrease (0.0-1.0, where 1.0 is no decay)")
    reasoning: str = Field(description="Why this specific decay rate was chosen based on the field and age")

class EvidenceStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Evidence Integration Stage")

        hypotheses = context.get("hypotheses", [])
        if not hypotheses:
            logger.warning("No hypotheses found from Stage 3")
            return {
                "summary": "Evidence integration skipped: No hypotheses available",
                "metrics": {}
            }

        parameters = context.get("parameters", {})
        max_iterations = parameters.get("evidence_max_iterations", 5)

        evidence_nodes_created = 0
        ibns_created = 0
        hyperedges_created = 0
        confidence_updates = 0

        for iteration in range(max_iterations):
            logger.info(f"Evidence integration iteration {iteration+1}/{max_iterations}")

            h_star, h_star_id = self._select_next_hypothesis(graph, hypotheses, parameters)
            if not h_star:
                logger.info("No more hypotheses to evaluate")
                break

            evidence_result = self._execute_plan(graph, h_star, parameters)

            for e_data in evidence_result.get("evidence", []):
                evidence_node = self._create_evidence_node(e_data, h_star_id)
                graph.add_node(evidence_node)

                edge_type = self._determine_edge_type(evidence_node, h_star)

                edge = Edge(
                    edge_id=f"e_{evidence_node.node_id}_{h_star_id}",
                    source=evidence_node.node_id,
                    target=h_star_id,
                    edge_type=edge_type["type"],
                    confidence=evidence_node.confidence[0],
                    metadata={
                        "edge_subtype": edge_type.get("subtype", ""),
                        "causal_metadata": edge_type.get("causal_metadata", {}),
                        "temporal_metadata": edge_type.get("temporal_metadata", {})
                    }
                )

                graph.add_edge(edge)
                evidence_nodes_created += 1

                old_confidence = h_star["confidence"]
                new_confidence = bayesian_update(
                    old_confidence,
                    evidence_node.confidence,
                    evidence_node.metadata.get("statistical_power", 0.5),
                    edge_type["type"]
                )

                graph.update_node_confidence(h_star_id, new_confidence)
                confidence_updates += 1

                if self._should_create_ibn(evidence_node, h_star):
                    ibn_id = graph.create_interdisciplinary_bridge(evidence_node.node_id, h_star_id)
                    if ibn_id:
                        ibns_created += 1

            potential_hyperedges = self._identify_potential_hyperedges(graph, evidence_result.get("evidence", []), h_star_id)
            for h_edge_data in potential_hyperedges:
                hyperedge = Hyperedge(
                    edge_id=f"hyper_{h_star_id}_{len(graph.hyperedges)}",
                    nodes=h_edge_data["nodes"],
                    confidence=h_edge_data["confidence"],
                    metadata=h_edge_data["metadata"]
                )

                graph.add_hyperedge(hyperedge)
                hyperedges_created += 1

            self._apply_temporal_decay(graph)

            temporal_patterns = self._detect_temporal_patterns(graph, h_star_id)
            for pattern in temporal_patterns:
                edge_id = pattern["edge_id"]
                temporal_metadata = pattern["temporal_metadata"]

                for u, v, data in graph.graph.edges(data=True):
                    if data.get("edge_id") == edge_id:
                        if "temporal_metadata" not in data:
                            data["temporal_metadata"] = {}

                        data["temporal_metadata"].update(temporal_metadata)

            self._adapt_topology(graph, h_star_id)

            bias_flags = self._detect_biases(graph, h_star_id, evidence_result)
            for bias in bias_flags:
                if bias["node_id"] in graph.graph:
                    if "bias_flags" not in graph.graph.nodes[bias["node_id"]]:
                        graph.graph.nodes[bias["node_id"]]["bias_flags"] = []

                    graph.graph.nodes[bias["node_id"]]["bias_flags"].append(bias["flag"])

            info_metrics = calculate_info_gain(graph, h_star_id, evidence_result)
            if h_star_id in graph.graph:
                graph.graph.nodes[h_star_id]["info_metrics"] = info_metrics

        graph.calculate_topology_metrics()

        logger.info(f"Evidence integration complete: {evidence_nodes_created} evidence nodes, {ibns_created} IBNs, {hyperedges_created} hyperedges")

        return {
            "summary": f"Integrated evidence nodes into the graph, updating hypothesis confidence and structure",
            "metrics": {
                "evidence_nodes_created": evidence_nodes_created,
                "ibns_created": ibns_created,
                "hyperedges_created": hyperedges_created,
                "confidence_updates": confidence_updates
            }
        }

    def _select_next_hypothesis(self, graph, hypotheses, parameters):
        if not hypotheses:
            return None, None

        best_score = -1
        best_h = None
        best_h_id = None

        for h_id in hypotheses:
            if h_id not in graph.graph:
                continue

            h = graph.graph.nodes[h_id]

            confidence = h.get("confidence", [0.5, 0.5, 0.5, 0.5])
            impact = h.get("impact_score", 0.5)
            cost = h.get("metadata", {}).get("computational_cost", 1.0)

            confidence_variance = sum((c - 0.5)**2 for c in confidence) / len(confidence)
            score = (impact * (1 - confidence_variance)) / cost

            if score > best_score:
                best_score = score
                best_h = h
                best_h_id = h_id

        return best_h, best_h_id

    def _execute_plan(self, graph, hypothesis, parameters):
        """通过 AI Agent 执行验证计划并获取真实证据"""
        plan = hypothesis.get("metadata", {}).get("plan", {})
        plan_type = "search"

        if isinstance(plan, dict):
            plan_type = plan.get("type", "search")

        description = hypothesis.get("description", "")
        falsification = hypothesis.get("falsification_criteria", "")
        disciplinary_tags = hypothesis.get("disciplinary_tags", [])

        # 定义一个用于提取结构化证据的系统提示词
        system_prompt: str = f"""你是一个严谨的科学验证助手。
        你的任务是根据给定的假设和验证计划，通过工具获取信息，并总结出关键证据。
        
        请输出以下 JSON 格式的结果：
        {{
            "content": "证据的详细总结，包含关键数据和发现",
            "source": "证据来源（如：PubMed, Web Search, Python Simulation）",
            "confidence_score": 0.0-1.0 之间的浮点数，表示证据的可靠性",
            "disciplinary_tags": ["相关的学科标签"],
            "statistical_power": 0.0-1.0 之间的浮点数，表示统计效力"
        }}
        """

        evidence = []

        try:
            if plan_type == "search":
                agent = create_agent(
                    system_prompt=system_prompt,
                    model=simple_chat_model,
                    tools=[build_web_search_tool()],
                )

                query = f"Search for scientific evidence regarding: {description}. Focus on: {falsification}"
                result = agent.invoke({"messages": [HumanMessage(content=query)]})

                # 尝试从 Agent 的最后一条消息中提取 JSON
                last_msg = result["messages"][-1].content
                # 简单的 JSON 提取逻辑（实际项目中建议使用更稳健的解析器）
                if "{" in last_msg:
                    start_idx = last_msg.index("{")
                    end_idx = last_msg.rindex("}") + 1
                    evidence_data = json.loads(last_msg[start_idx:end_idx])

                    evidence.append({
                        "content": evidence_data.get("content", last_msg),
                        "source": evidence_data.get("source", "web_search"),
                        "confidence": [evidence_data.get("confidence_score", 0.7)] * 4,
                        "disciplinary_tags": disciplinary_tags,
                        "statistical_power": evidence_data.get("statistical_power", 0.6)
                    })

            elif plan_type == "experiment":
                agent = create_agent(
                    system_prompt=system_prompt,
                    model=simple_chat_model,
                    tools=[build_python_repl_tool()],
                )

                query = f"Write and execute a Python simulation to test this hypothesis: {description}. Return the results in the required JSON format."
                result = agent.invoke({"messages": [HumanMessage(content=query)]})

                last_msg = result["messages"][-1].content
                if "{" in last_msg:
                    start_idx = last_msg.index("{")
                    end_idx = last_msg.rindex("}") + 1
                    evidence_data = json.loads(last_msg[start_idx:end_idx])

                    evidence.append({
                        "content": evidence_data.get("content", "Simulation completed"),
                        "source": "python_simulation",
                        "confidence": [evidence_data.get("confidence_score", 0.8)] * 4,
                        "disciplinary_tags": disciplinary_tags,
                        "statistical_power": evidence_data.get("statistical_power", 0.75)
                    })
            else:
                # 默认使用 LLM 进行逻辑推演
                result = simple_chat_model.invoke(
                    [system_prompt, HumanMessage(content=f"Logically evaluate: {description}")])
                evidence.append({
                    "content": result.content,
                    "source": "logical_deduction",
                    "confidence": [0.6] * 4,
                    "disciplinary_tags": disciplinary_tags,
                    "statistical_power": 0.5
                })

        except Exception as e:
            logger.error(f"Failed to execute plan via AI: {e}")
            # 降级处理：如果 AI 失败，返回一个简单的错误证据
            evidence.append({
                "content": f"AI execution failed: {str(e)}",
                "source": "error_log",
                "confidence": [0.1] * 4,
                "disciplinary_tags": [],
                "statistical_power": 0.0
            })

        return {
            "evidence": evidence,
            "execution_success": len(evidence) > 0
        }

    def _create_evidence_node(self, evidence_data, hypothesis_id):
        node_id = f"e_{hypothesis_id}_{random.randint(1000, 9999)}"

        return Node(
            node_id=node_id,
            label=evidence_data.get("content", "Evidence"),
            node_type="evidence",
            confidence=evidence_data.get("confidence", [0.7, 0.7, 0.7, 0.7]),
            metadata={
                "source": evidence_data.get("source", "unknown"),
                "timestamp": str(datetime.datetime.now()),
                "disciplinary_tags": evidence_data.get("disciplinary_tags", []),
                "statistical_power": evidence_data.get("statistical_power", 0.5),
                "related_hypothesis": hypothesis_id
            }
        )

    def _determine_edge_type(self, evidence_node, hypothesis):
        """使用 AI 分析证据与假设之间的逻辑关系"""
        try:
            # 准备输入内容
            evidence_content = evidence_node.label
            hypothesis_desc = hypothesis.get("description", "")

            prompt = HumanMessage(content=f"""
                    Analyze the logical relationship between the following evidence and hypothesis.

                    Hypothesis: {hypothesis_desc}
                    Evidence: {evidence_content}

                    Determine the edge type based on scientific logic.
                    """)

            # 调用模型并指定响应格式
            result = simple_chat_model.with_structured_output(EdgeTypeOutput).invoke([prompt])

            if isinstance(result, EdgeTypeOutput):
                return {
                    "type": result.type,
                    "subtype": result.subtype,
                    "causal_metadata": result.causal_metadata,
                    "temporal_metadata": result.temporal_metadata,
                }
        except Exception as e:
            logger.warning(f"AI edge determination failed, falling back to default: {e}")

        # 降级处理：如果 AI 失败，返回默认的支持关系
        return {
            "type": "supportive",
            "subtype": None,
            "causal_metadata": {},
            "temporal_metadata": {}
        }

    def _should_create_ibn(self, evidence_node, hypothesis):
        try:
            evidence_tags = evidence_node.metadata.get("disciplinary_tags", [])
            hypothesis_tags = hypothesis.get("disciplinary_tags", [])

            # 如果任何一方没有标签，直接返回 False（无法判断跨学科性）
            if not evidence_tags or not hypothesis_tags:
                return False

            prompt = HumanMessage(content=f"""
                    Determine if a meaningful interdisciplinary bridge exists between the following sets of tags.

                    Hypothesis Disciplines: {hypothesis_tags}
                    Evidence Disciplines: {evidence_tags}

                    A bridge is needed if there is a non-trivial, insightful connection between these distinct fields that enhances the understanding of the hypothesis.
                    """)

            result = simple_chat_model.with_structured_output(IBNDecision).invoke([prompt])

            if isinstance(result, IBNDecision):
                # 只有当 AI 认为需要建立桥梁，且强度超过一定阈值时才返回 True
                return result.should_create and result.bridge_strength > 0.6
        except Exception as e:
            logger.warning(f"AI IBN decision failed, falling back to tag matching: {e}")

        evidence_tags_set = set(evidence_node.metadata.get("disciplinary_tags", []))
        hypothesis_tags_set = set(hypothesis.get("disciplinary_tags", []))

        return len(evidence_tags_set.intersection(hypothesis_tags_set)) == 0

    def _identify_potential_hyperedges(self, graph, evidence_data, hypothesis_id):
        hyperedges = []

        if len(evidence_data) >= 2:
            hyperedges.append({
                "nodes": [hypothesis_id] + [f"e_{hypothesis_id}_{random.randint(1000, 9999)}" for _ in range(2)],
                "confidence": 0.7,
                "metadata": {
                    "relationship": "joint_support",
                    "timestamp": str(datetime.datetime.now())
                }
            })

        return hyperedges

    def _apply_temporal_decay(self, graph):
        """使用 AI 根据证据的时效性和学科特性应用置信度衰减"""
        try:
            # 获取所有证据节点
            evidence_nodes = [
                (nid, ndata) for nid, ndata in graph.graph.nodes(data=True)
                if ndata.get("node_type") == "evidence"
            ]

            if not evidence_nodes:
                return

            # 提取节点信息供 AI 评估
            node_info = []
            for nid, ndata in evidence_nodes[:10]:  # 限制处理数量以保持性能
                timestamp = ndata.get("metadata", {}).get("timestamp", "")
                tags = ndata.get("metadata", {}).get("disciplinary_tags", [])
                node_info.append(f"- ID: {nid}, Time: {timestamp}, Tags: {tags}")

            prompt = HumanMessage(content=f"""
            Evaluate the temporal relevance of the following evidence nodes.

            Current Date: {datetime.datetime.now().strftime('%Y-%m-%d')}

            Nodes to evaluate:
            {chr(10).join(node_info)}

            For each node, determine a 'decay_rate' (0.0 to 1.0):
            - 1.0: No decay (timeless truth, e.g., fundamental math/physics).
            - 0.9-0.99: Slow decay (stable sciences like biology).
            - <0.9: Fast decay (rapidly changing fields like AI or clinical medicine).

            Return a list of nodes that need their confidence adjusted.
            """)

            structured_llm = simple_chat_model.with_structured_output(List[DecayFactor])
            result = structured_llm.invoke([prompt])

            if isinstance(result, list):
                for df in result:
                    if df.node_id in graph.graph:
                        current_conf = graph.graph.nodes[df.node_id].get("confidence", [0.5] * 4)
                        # 应用衰减：新置信度 = 旧置信度 * 衰减率
                        new_conf = [c * df.decay_rate for c in current_conf]
                        graph.update_node_confidence(df.node_id, new_conf)
                        logger.debug(f"Applied decay {df.decay_rate} to node {df.node_id}: {df.reasoning}")

        except Exception as e:
            logger.warning(f"AI temporal decay application failed: {e}")

    def _detect_temporal_patterns(self, graph, hypothesis_id):
        """使用 AI 从证据中提取时序模式和延迟特征"""
        patterns = []

        try:
            # 获取与该假设相连的所有证据边
            edges_data = []
            for u, v, data in graph.graph.edges(data=True):
                if v == hypothesis_id or u == hypothesis_id:
                    # 找到对应的证据节点内容
                    source_id = u if v == hypothesis_id else v
                    source_node = graph.graph.nodes.get(source_id, {})
                    if source_node.get("node_type") == "evidence":
                        edges_data.append({
                            "edge_id": data.get("edge_id"),
                            "content": source_node.get("label", "")
                        })

            if not edges_data:
                return patterns

            prompt = HumanMessage(content=f"""
                    Analyze the following evidence related to hypothesis '{hypothesis_id}' for temporal patterns.

                    Evidence Items:
                    {json.dumps(edges_data, indent=2)}

                    For each piece of evidence that contains temporal information, identify:
                    1. The type of timing (immediate, delayed, etc.)
                    2. The specific delay or duration mentioned.
                    3. The trend observed over that period.

                    Return a list of detected patterns.
                    """)

            structured_llm = simple_chat_model.with_structured_output(List[TemporalPattern])
            result = structured_llm.invoke([prompt])

            if isinstance(result, list):
                for p in result:
                    if isinstance(p, TemporalPattern):
                        patterns.append({
                            "edge_id": p.edge_id,
                            "temporal_metadata": {
                                "pattern_type": p.pattern_type,
                                "delay": p.delay_description,
                                "trend": p.trend
                            }
                        })
        except Exception as e:
            logger.warning(f"AI temporal pattern detection failed: {e}")

        return patterns

    def _adapt_topology(self, graph, hypothesis_id):
        """使用 AI 根据最新证据动态调整图谱拓扑结构"""
        try:
            # 获取当前假设及其邻居节点的信息
            hypothesis_node = graph.graph.nodes.get(hypothesis_id, {})
            neighbors = list(graph.graph.neighbors(hypothesis_id))

            # 提取邻居节点的摘要信息供 AI 参考
            neighbor_info = []
            for n_id in neighbors[:5]:  # 限制数量防止 Prompt 过长
                n_data = graph.graph.nodes.get(n_id, {})
                neighbor_info.append(f"- {n_id}: type={n_data.get('node_type')}, label={n_data.get('label')}")

            prompt = HumanMessage(content=f"""
            Analyze the local topology around hypothesis '{hypothesis_id}' and suggest structural adaptations.

            Current Hypothesis Confidence: {hypothesis_node.get('confidence')}
            Connected Nodes:
            {chr(10).join(neighbor_info)}

            Suggest one primary action:
            - 'prune': If the hypothesis is sufficiently falsified.
            - 'merge': If it overlaps significantly with a neighbor.
            - 'reweight': If the importance of connections needs updating.
            - 'create_link': If a new logical path is evident.

            Return the most scientifically sound structural adjustment.
            """)

            structured_llm = simple_chat_model.with_structured_output(TopologyAction)
            result = structured_llm.invoke([prompt])

            if isinstance(result, TopologyAction):
                logger.info(f"Topology adaptation suggested: {result.action} for {result.target_node_id}")

                # 执行具体的图操作
                if result.action == "prune" and result.target_node_id in graph.graph:
                    # 这里可以调用 graph 的删除逻辑，或者降低权重
                    graph.graph.nodes[result.target_node_id]["status"] = "pruned"

                elif result.action == "merge":
                    # 执行合并逻辑（需要 graph 支持）
                    pass

        except Exception as e:
            logger.warning(f"AI topology adaptation failed: {e}")

    def _detect_biases(self, graph, hypothesis_id, evidence_result):
        """使用 AI 检测验证过程中可能存在的认知偏见"""
        biases = []

        try:
            # 获取假设信息和新生成的证据
            hypothesis_node = graph.graph.nodes.get(hypothesis_id, {})
            hypothesis_desc = hypothesis_node.get("description", "")

            # 提取所有新证据的内容
            evidence_contents = [e.get("content", "") for e in evidence_result.get("evidence", [])]

            if not evidence_contents:
                return biases

            prompt = HumanMessage(content=f"""
                    Act as a critical peer reviewer. Analyze the following hypothesis and the newly gathered evidence for potential cognitive or methodological biases.

                    Hypothesis: {hypothesis_desc}
                    New Evidence: {evidence_contents}

                    Common biases to look for:
                    1. Confirmation Bias: Is the evidence only supporting the hypothesis while ignoring contradictory data?
                    2. Sampling Bias: Is the evidence drawn from a limited or unrepresentative source?
                    3. Anchoring Bias: Is the reasoning overly dependent on the first piece of information?

                    Return a list of any detected biases. If no significant bias is found, return an empty list.
                    """)

            # 使用 with_structured_output 处理列表输出
            structured_llm = simple_chat_model.with_structured_output(List[BiasFlag])
            result = structured_llm.invoke([prompt])

            if isinstance(result, list):
                for bias in result:
                    if isinstance(bias, BiasFlag):
                        biases.append({
                            "node_id": bias.node_id or hypothesis_id,
                            "flag": bias.flag_type,
                            "description": bias.description,
                            "severity": bias.severity
                        })
        except Exception as e:
            logger.warning(f"AI bias detection failed: {e}")

        return biases