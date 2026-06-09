import logging
import datetime
from typing import Dict, Any, List

from models import simple_chat_model
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from future.ast_got.models.graph import AGoTGraph

logger = logging.getLogger("agot-stage7")


# ============================================================
# Pydantic 模型：AI 结构化输出
# ============================================================

class ExecutiveSummary(BaseModel):
    """AI 生成的执行摘要"""
    summary: str = Field(description="A comprehensive 3-5 sentence executive summary covering key findings, the scope of analysis, and the main conclusions drawn from the knowledge graph")


class SectionAnalysis(BaseModel):
    """AI 对某个子图的分析段落"""
    content: str = Field(description="A detailed analysis paragraph (3-8 sentences) describing the key findings, patterns, and insights from this subgraph section")


class InterdisciplinaryInsight(BaseModel):
    """AI 综合的跨学科洞察"""
    insight: str = Field(description="A synthesis paragraph (3-6 sentences) that identifies cross-disciplinary patterns, knowledge transfer opportunities, and holistic insights that emerge from connecting multiple disciplines")


class GapsAnalysis(BaseModel):
    """AI 识别的知识缺口与研究机会"""
    content: str = Field(description="An analysis paragraph (3-6 sentences) identifying knowledge gaps, points of high uncertainty, conflicting evidence, and promising research directions")


class CompositionStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Composition Stage")

        subgraphs = context.get("subgraphs", [])
        if not subgraphs:
            logger.warning("No subgraphs found for composition")
            return {
                "summary": "Composition skipped: No subgraphs available",
                "metrics": {}
            }

        parameters = context.get("parameters", {})
        query = context.get("query", "")

        output_sections = []
        citations = []

        # --- [AI] 执行摘要 ---
        executive_summary = self._generate_executive_summary(graph, subgraphs, query)
        output_sections.append({
            "title": "Executive Summary",
            "content": executive_summary,
            "type": "summary"
        })

        # --- [AI] 逐子图分析 ---
        for subgraph_data in subgraphs:
            section_content = self._generate_section_from_subgraph(
                graph,
                subgraph_data,
                citations
            )

            output_sections.append({
                "title": f"{subgraph_data.get('name', 'Unnamed').replace('_', ' ').title()} Analysis",
                "content": section_content,
                "type": "analysis",
                "subgraph": subgraph_data.get("name")
            })

        # --- [AI] 跨学科洞察 ---
        if graph.ibns:
            ibn_insights = self._generate_interdisciplinary_insights(graph)
            output_sections.append({
                "title": "Interdisciplinary Insights",
                "content": ibn_insights,
                "type": "interdisciplinary"
            })

        # --- [AI] 知识缺口 ---
        knowledge_gaps = self._identify_knowledge_gaps(graph)
        if knowledge_gaps:
            output_sections.append({
                "title": "Knowledge Gaps and Research Opportunities",
                "content": knowledge_gaps,
                "type": "gaps"
            })

        formatted_citations = self._format_citations_vancouver(citations)

        composition_result = {
            "title": f"AGoT Analysis: {query[:50]}{'...' if len(query) > 50 else ''}",
            "timestamp": str(datetime.datetime.now()),
            "sections": output_sections,
            "citations": formatted_citations,
            "node_count": graph.graph.number_of_nodes(),
            "edge_count": graph.graph.number_of_edges(),
            "hyperedge_count": len(graph.hyperedges),
            "ibn_count": len(graph.ibns)
        }

        logger.info(f"Composition complete: Generated output with {len(output_sections)} sections")

        return {
            "composition_result": composition_result,
            "summary": f"Composed final output with {len(output_sections)} sections and {len(formatted_citations)} citations",
            "metrics": {
                "section_count": len(output_sections),
                "citation_count": len(formatted_citations)
            }
        }

    def _generate_executive_summary(self, graph: AGoTGraph,
                                  subgraphs: List[Dict[str, Any]],
                                  query: str) -> str:
        """生成执行摘要：优先 AI，失败时回退到模板"""
        ai_result = self._ai_executive_summary(graph, subgraphs, query)
        if ai_result is not None:
            return ai_result

        # Fallback: template-based
        node_count = graph.graph.number_of_nodes()
        edge_count = graph.graph.number_of_edges()
        subgraph_count = len(subgraphs)

        summary = (
            f"This analysis explores the query: \"{query}\". "
            f"The AGoT framework generated a knowledge graph with {node_count} nodes and {edge_count} edges, "
            f"from which {subgraph_count} focused subgraphs were extracted for analysis. "
            f"Key findings include insights from "
        )

        subgraph_names = [sg.get("name", "unnamed").replace("_", " ") for sg in subgraphs]
        if len(subgraph_names) > 1:
            summary += ", ".join(subgraph_names[:-1]) + " and " + subgraph_names[-1] + " perspectives. "
        else:
            summary += subgraph_names[0] + " perspective. "

        if graph.ibns:
            summary += f"The analysis identified {len(graph.ibns)} interdisciplinary bridge concepts connecting different domains. "

        summary += (
            f"Confidence in these findings is based on multi-dimensional evaluation across empirical, "
            f"theoretical, methodological, and consensus dimensions."
        )

        return summary

    def _generate_section_from_subgraph(self, graph: AGoTGraph,
                                      subgraph_data: Dict[str, Any],
                                      citations: List[Dict[str, Any]]) -> str:
        """生成子图分析段落：优先 AI，失败时回退到模板"""
        ai_result = self._ai_section_analysis(graph, subgraph_data, citations)
        if ai_result is not None:
            return ai_result

        # Fallback: template-based
        nodes = subgraph_data.get("nodes", [])
        description = subgraph_data.get("description", "")

        content = f"{description}\n\n"

        high_conf_nodes = []
        for node_id in nodes:
            if node_id in graph.graph:
                node_data = graph.graph.nodes[node_id]
                confidence = node_data.get("confidence", [])
                if confidence and sum(confidence) / len(confidence) >= 0.7:
                    high_conf_nodes.append((node_id, node_data))

        high_conf_nodes.sort(key=lambda x: -sum(x[1].get("confidence", [0])) / len(x[1].get("confidence", [1])))

        for node_id, node_data in high_conf_nodes[:5]:
            label = node_data.get("label", "Unlabeled Node")
            node_type = node_data.get("node_type", "unknown")

            if node_type == "hypothesis":
                citation_id = len(citations) + 1
                citations.append({
                    "id": citation_id,
                    "node_id": node_id,
                    "type": "node",
                    "label": label,
                    "node_type": node_type,
                    "timestamp": node_data.get("timestamp", "")
                })

                content += f"• {label} [Node {node_id}][{citation_id}]\n"

                evidence_nodes = []
                for u, v in graph.graph.in_edges(node_id):
                    if u in graph.graph and graph.graph.nodes[u].get("node_type") == "evidence":
                        evidence_nodes.append((u, graph.graph.nodes[u]))

                if evidence_nodes:
                    content += "  Supporting evidence:\n"
                    for ev_id, ev_data in evidence_nodes[:3]:
                        ev_label = ev_data.get("label", "Unlabeled Evidence")

                        ev_citation_id = len(citations) + 1
                        citations.append({
                            "id": ev_citation_id,
                            "node_id": ev_id,
                            "type": "node",
                            "label": ev_label,
                            "node_type": "evidence",
                            "timestamp": ev_data.get("timestamp", ""),
                            "source": ev_data.get("source", "")
                        })

                        content += f"    - {ev_label} [{ev_citation_id}]\n"

        return content

    def _generate_interdisciplinary_insights(self, graph: AGoTGraph) -> str:
        """生成跨学科洞察：优先 AI，失败时回退到模板"""
        ai_result = self._ai_interdisciplinary_insights(graph)
        if ai_result is not None:
            return ai_result

        # Fallback: template-based
        if not graph.ibns:
            return "No interdisciplinary connections identified."

        content = f"Analysis identified {len(graph.ibns)} interdisciplinary bridge concepts:\n\n"

        for ibn_id in graph.ibns:
            if ibn_id in graph.graph:
                ibn_data = graph.graph.nodes[ibn_id]
                label = ibn_data.get("label", "Unlabeled Bridge")

                source_disciplines = ibn_data.get("source_disciplines", [])
                target_disciplines = ibn_data.get("target_disciplines", [])

                content += f"• {label} [Node {ibn_id}]\n"
                content += f"  Connects disciplines: {', '.join(source_disciplines)} ↔ {', '.join(target_disciplines)}\n"

        return content

    def _identify_knowledge_gaps(self, graph: AGoTGraph) -> str:
        """识别知识缺口：优先 AI，失败时回退到规则"""
        ai_result = self._ai_identify_gaps(graph)
        if ai_result is not None:
            return ai_result

        # Fallback: rule-based
        gap_nodes = []

        for node_id, data in graph.graph.nodes(data=True):
            if data.get("node_type") == "placeholder_gap":
                gap_nodes.append((node_id, data, "explicit"))
                continue

            confidence = data.get("confidence", [])
            if len(confidence) >= 2:
                variance = sum((c - sum(confidence)/len(confidence))**2 for c in confidence) / len(confidence)
                if variance > 0.1:
                    gap_nodes.append((node_id, data, "variance"))

        if not gap_nodes:
            return "No significant knowledge gaps identified."

        content = "The following knowledge gaps and research opportunities were identified:\n\n"

        for node_id, data, gap_type in gap_nodes:
            label = data.get("label", "Unlabeled Gap")

            if gap_type == "explicit":
                content += f"• {label} [Node {node_id}] - Explicit knowledge gap\n"
            else:
                content += f"• {label} [Node {node_id}] - High uncertainty (confidence variance)\n"

            if "research_questions" in data:
                content += "  Suggested research questions:\n"
                for question in data["research_questions"]:
                    content += f"    - {question}\n"

        return content

    # ============================================================
    # AI 辅助方法
    # ============================================================

    def _ai_executive_summary(self, graph: AGoTGraph,
                              subgraphs: List[Dict[str, Any]],
                              query: str) -> str:
        """[AI] 生成执行摘要"""
        try:
            node_count = graph.graph.number_of_nodes()
            edge_count = graph.graph.number_of_edges()
            ibn_count = len(graph.ibns)

            subgraph_info = []
            for sg in subgraphs[:8]:
                subgraph_info.append(
                    f"- {sg.get('name', 'unnamed')}: {sg.get('metrics', {}).get('node_count', 0)} nodes, "
                    f"{sg.get('metrics', {}).get('edge_count', 0)} edges"
                )

            prompt = HumanMessage(content=f"""
Generate an executive summary for a knowledge-graph-based analysis.

Research Query: "{query}"

Graph Statistics:
- Total nodes: {node_count}
- Total edges: {edge_count}
- Interdisciplinary bridges: {ibn_count}
- Subgraphs extracted: {len(subgraphs)}

Extracted Subgraphs:
{chr(10).join(subgraph_info)}

Write a concise 3-5 sentence executive summary covering:
1. The scope and approach of the analysis
2. Key findings from the extracted subgraphs
3. Main conclusions and their confidence level
""")
            result = simple_chat_model.with_structured_output(ExecutiveSummary).invoke([prompt])
            if isinstance(result, ExecutiveSummary) and result.summary:
                return result.summary
        except Exception as e:
            logger.warning(f"AI executive summary failed: {e}")
        return None

    def _ai_section_analysis(self, graph: AGoTGraph,
                             subgraph_data: Dict[str, Any],
                             citations: List[Dict[str, Any]]) -> str:
        """[AI] 分析单个子图并生成段落"""
        try:
            nodes = subgraph_data.get("nodes", [])
            description = subgraph_data.get("description", "")

            high_conf_nodes = []
            for node_id in nodes:
                if node_id in graph.graph:
                    node_data = graph.graph.nodes[node_id]
                    nd = graph.graph.nodes[node_id]
                    confidence = node_data.get("confidence", [])
                    avg_conf = sum(confidence) / len(confidence) if confidence else 0

                    if avg_conf >= 0.7:
                        high_conf_nodes.append((node_id, node_data, avg_conf))

            high_conf_nodes.sort(key=lambda x: -x[2])
            top_nodes = high_conf_nodes[:8]

            node_descriptions = []
            for nid, nd, conf in top_nodes:
                label = nd.get("label", "")[:80]
                ntype = nd.get("node_type", "")
                tags = nd.get("disciplinary_tags", [])
                impact = nd.get("impact_score", "N/A")
                meta_desc = nd.get("metadata", {}).get("description", "")[:150]

                is_hypothesis = ntype == "hypothesis"
                evidence_count = 0
                if is_hypothesis:
                    for u in graph.graph.predecessors(nid):
                        if graph.graph.nodes[u].get("node_type") == "evidence":
                            evidence_count += 1

                node_descriptions.append(
                    f"- [{nid}] Label: {label}\n"
                    f"  Type: {ntype} | Confidence: {conf:.2f} | Impact: {impact}\n"
                    f"  Tags: {tags}\n"
                    f"  Description: {meta_desc}\n"
                    f"  Evidence count: {evidence_count}"
                )

            prompt = HumanMessage(content=f"""
Analyze this subgraph from a larger knowledge graph analysis. Generate a coherent analysis paragraph.

Research Context: The subgraph is labeled as "{subgraph_data.get('name', 'unnamed')}"
Subgraph description: {description}

Key nodes in this subgraph (highest confidence):
{chr(10).join(node_descriptions)}

Write a 3-8 sentence analysis paragraph that:
1. Describes the main patterns evident in this subgraph
2. Highlights the most important nodes and their connections
3. Draws meaningful conclusions relevant to the research context
4. Notes any supporting evidence structures
""")
            result = simple_chat_model.with_structured_output(SectionAnalysis).invoke([prompt])
            if isinstance(result, SectionAnalysis) and result.content:
                # Extract citations from the AI output (hypothesis nodes)
                # This keeps the citation collection working for the bibliography
                for nid, nd, _ in top_nodes:
                    nd_data = graph.graph.nodes[nid]
                    if nd_data.get("node_type") == "hypothesis":
                        cid = len(citations) + 1
                        citations.append({
                            "id": cid,
                            "node_id": nid,
                            "type": "node",
                            "label": nd_data.get("label", ""),
                            "node_type": "hypothesis",
                            "timestamp": nd_data.get("timestamp", "")
                        })
                        # Collect evidence citations
                        for u in graph.graph.predecessors(nid):
                            if u in graph.graph and graph.graph.nodes[u].get("node_type") == "evidence":
                                ev_data = graph.graph.nodes[u]
                                ecid = len(citations) + 1
                                citations.append({
                                    "id": ecid,
                                    "node_id": u,
                                    "type": "node",
                                    "label": ev_data.get("label", ""),
                                    "node_type": "evidence",
                                    "timestamp": ev_data.get("timestamp", ""),
                                    "source": ev_data.get("source", "")
                                })
                return result.content
        except Exception as e:
            logger.warning(f"AI section analysis failed: {e}")
        return None

    def _ai_interdisciplinary_insights(self, graph: AGoTGraph) -> str:
        """[AI] 综合生成跨学科洞察"""
        try:
            ibn_details = []
            for ibn_id in graph.ibns:
                if ibn_id in graph.graph:
                    ibn_data = graph.graph.nodes[ibn_id]
                    label = ibn_data.get("label", "Unlabeled Bridge")[:100]
                    source_disc = ibn_data.get("source_disciplines", [])
                    target_disc = ibn_data.get("target_disciplines", [])
                    meta_desc = ibn_data.get("metadata", {}).get("description", "")[:200]

                    # Find connected nodes
                    connected = []
                    for n in graph.graph.neighbors(ibn_id):
                        if n in graph.graph:
                            connected.append(graph.graph.nodes[n].get("label", n)[:60])

                    ibn_details.append(
                        f"- Bridge: {label} [Node {ibn_id}]\n"
                        f"  Connects: {', '.join(source_disc)} ↔ {', '.join(target_disc)}\n"
                        f"  Description: {meta_desc}\n"
                        f"  Connected to: {', '.join(connected[:5])}"
                    )

            prompt = HumanMessage(content=f"""
Synthesize interdisciplinary insights from a knowledge graph containing {len(graph.ibns)} bridge concepts that connect different academic disciplines.

Interdisciplinary Bridges:
{chr(10).join(ibn_details)}

Write a 3-6 sentence synthesis paragraph that:
1. Identifies the overarching interdisciplinary themes
2. Explains how different disciplinary perspectives complement each other
3. Highlights novel insights that emerge from the cross-disciplinary connections
4. Suggests implications that span across traditional disciplinary boundaries
""")
            result = simple_chat_model.with_structured_output(InterdisciplinaryInsight).invoke([prompt])
            if isinstance(result, InterdisciplinaryInsight) and result.insight:
                return result.insight
        except Exception as e:
            logger.warning(f"AI interdisciplinary insights failed: {e}")
        return None

    def _ai_identify_gaps(self, graph: AGoTGraph) -> str:
        """[AI] 分析并识别知识缺口"""
        try:
            # Collect rule-based gap data
            gap_nodes_raw = []
            for node_id, data in graph.graph.nodes(data=True):
                if data.get("node_type") == "placeholder_gap":
                    gap_nodes_raw.append((node_id, data, "explicit"))
                    continue
                confidence = data.get("confidence", [])
                if len(confidence) >= 2:
                    variance = sum((c - sum(confidence)/len(confidence))**2 for c in confidence) / len(confidence)
                    if variance > 0.1:
                        gap_nodes_raw.append((node_id, data, "variance"))

            # Collect graph-wide metadata
            node_types = {}
            for _, data in graph.graph.nodes(data=True):
                nt = data.get("node_type", "unknown")
                node_types[nt] = node_types.get(nt, 0) + 1

            gap_details = []
            for nid, data, gtype in gap_nodes_raw[:8]:
                label = data.get("label", "")[:80]
                gap_details.append(
                    f"- {label} [{nid}] type={gtype}"
                    f" | tags={data.get('disciplinary_tags', [])}"
                )

            prompt = HumanMessage(content=f"""
Analyze knowledge gaps in this research knowledge graph.

Graph node type distribution: {node_types}
Total knowledge gaps identified by rule-based detection: {len(gap_nodes_raw)}
Total nodes: {graph.graph.number_of_nodes()}

Known gaps:
{chr(10).join(gap_details) if gap_details else "(no explicit gaps found)"}

Generate a 3-6 sentence analysis that:
1. Characterizes the nature of knowledge gaps present
2. Identifies which domains or disciplines have the most uncertainty
3. Suggests promising research directions to fill these gaps
4. Highlights areas where additional evidence would strengthen conclusions
""")
            result = simple_chat_model.with_structured_output(GapsAnalysis).invoke([prompt])
            if isinstance(result, GapsAnalysis) and result.content:
                return result.content
        except Exception as e:
            logger.warning(f"AI gap analysis failed: {e}")
        return None

    def _format_citations_vancouver(self, citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """纯格式化逻辑——无需 AI"""
        formatted = []

        for citation in citations:
            if citation.get("type") == "node":
                formatted.append({
                    "id": citation["id"],
                    "text": f"{citation.get('label', 'Unlabeled')}. AGoT Node {citation.get('node_id', 'unknown')}. "
                           f"Type: {citation.get('node_type', 'unknown')}. "
                           f"Generated: {citation.get('timestamp', '')}."
                })
            else:
                formatted.append({
                    "id": citation["id"],
                    "text": f"{citation.get('label', 'Unlabeled')}. {citation.get('source', 'Unknown source')}. "
                           f"{citation.get('timestamp', '')}."
                })

        return formatted
