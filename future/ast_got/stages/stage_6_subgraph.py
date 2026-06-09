import textwrap
from loguru import logger
from typing import Dict, Any, List, Tuple

from models import simple_chat_model
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage

from future.ast_got.models.graph import AGoTGraph


class SubgraphLabel(BaseModel):
    """AI 为子图生成的有意义的名称和描述"""
    name: str = Field(description="A concise snake_case name for this subgraph (e.g. 'core_empirical_evidence')")
    description: str = Field(description="A 1-2 sentence description of what this subgraph represents in context of the query")


class SubgraphStage:
    def execute(self, graph: AGoTGraph, context: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Executing Subgraph Extraction Stage")

        parameters = context.get("parameters", {})

        extraction_criteria = parameters.get("extraction_criteria", {})

        min_confidence = extraction_criteria.get("min_confidence", 0.6)
        min_impact = extraction_criteria.get("min_impact", 0.5)
        focus_disciplines = extraction_criteria.get("focus_disciplines", [])
        temporal_recency = extraction_criteria.get("temporal_recency", 0)
        edge_patterns = extraction_criteria.get("edge_patterns", [])
        focus_layers = extraction_criteria.get("focus_layers", [])

        query = context.get("query", "")
        extracted_subgraphs = []

        # --- Extract high-confidence subgraph ---
        high_confidence_nodes = []
        for node_id, data in graph.graph.nodes(data=True):
            confidence = data.get("confidence", [])
            if not confidence:
                continue

            avg_confidence = sum(confidence) / len(confidence)

            if avg_confidence >= min_confidence:
                high_confidence_nodes.append(node_id)

        high_confidence_subgraph = graph.graph.subgraph(high_confidence_nodes)
        if high_confidence_subgraph.number_of_nodes() > 0:
            subgraph_data = self._build_subgraph_entry(
                graph,
                base_name="high_confidence",
                base_desc=f"Subgraph containing nodes with confidence >= {min_confidence}",
                nodes=list(high_confidence_subgraph.nodes()),
                edges=list(high_confidence_subgraph.edges()),
                metrics={
                    "node_count": high_confidence_subgraph.number_of_nodes(),
                    "edge_count": high_confidence_subgraph.number_of_edges()
                },
                query=query
            )
            extracted_subgraphs.append(subgraph_data)

        # --- Extract high-impact subgraph ---
        high_impact_nodes = []
        for node_id, data in graph.graph.nodes(data=True):
            impact = data.get("impact_score", 0)

            if impact >= min_impact:
                high_impact_nodes.append(node_id)

        high_impact_subgraph = graph.graph.subgraph(high_impact_nodes)
        if high_impact_subgraph.number_of_nodes() > 0:
            subgraph_data = self._build_subgraph_entry(
                graph,
                base_name="high_impact",
                base_desc=f"Subgraph containing nodes with impact >= {min_impact}",
                nodes=list(high_impact_subgraph.nodes()),
                edges=list(high_impact_subgraph.edges()),
                metrics={
                    "node_count": high_impact_subgraph.number_of_nodes(),
                    "edge_count": high_impact_subgraph.number_of_edges()
                },
                query=query
            )
            extracted_subgraphs.append(subgraph_data)

        # --- Extract discipline-focused subgraph ---
        if focus_disciplines:
            discipline_nodes = []
            for node_id, data in graph.graph.nodes(data=True):
                tags = data.get("disciplinary_tags", [])

                if any(tag in focus_disciplines for tag in tags):
                    discipline_nodes.append(node_id)

            discipline_subgraph = graph.graph.subgraph(discipline_nodes)
            if discipline_subgraph.number_of_nodes() > 0:
                subgraph_data = self._build_subgraph_entry(
                    graph,
                    base_name="discipline_focus",
                    base_desc=f"Subgraph focused on disciplines: {', '.join(focus_disciplines)}",
                    nodes=list(discipline_subgraph.nodes()),
                    edges=list(discipline_subgraph.edges()),
                    metrics={
                        "node_count": discipline_subgraph.number_of_nodes(),
                        "edge_count": discipline_subgraph.number_of_edges()
                    },
                    query=query
                )
                extracted_subgraphs.append(subgraph_data)

        # --- Extract layer-focused subgraph ---
        if focus_layers and graph.layers:
            layer_nodes = []
            for layer_id in focus_layers:
                if layer_id in graph.layers:
                    layer_nodes.extend(graph.layers[layer_id])

            layer_subgraph = graph.graph.subgraph(layer_nodes)
            if layer_subgraph.number_of_nodes() > 0:
                subgraph_data = self._build_subgraph_entry(
                    graph,
                    base_name="layer_focus",
                    base_desc=f"Subgraph focused on layers: {', '.join(focus_layers)}",
                    nodes=list(layer_subgraph.nodes()),
                    edges=list(layer_subgraph.edges()),
                    metrics={
                        "node_count": layer_subgraph.number_of_nodes(),
                        "edge_count": layer_subgraph.number_of_edges()
                    },
                    query=query
                )
                extracted_subgraphs.append(subgraph_data)

        # --- Extract edge-pattern subgraph ---
        if edge_patterns:
            pattern_nodes = set()
            for u, v, data in graph.graph.edges(data=True):
                edge_type = data.get("edge_type", "")
                edge_subtype = data.get("edge_subtype", "")

                if edge_type in edge_patterns or edge_subtype in edge_patterns:
                    pattern_nodes.add(u)
                    pattern_nodes.add(v)

            pattern_subgraph = graph.graph.subgraph(pattern_nodes)
            if pattern_subgraph.number_of_nodes() > 0:
                subgraph_data = self._build_subgraph_entry(
                    graph,
                    base_name="edge_pattern",
                    base_desc=f"Subgraph containing edge patterns: {', '.join(edge_patterns)}",
                    nodes=list(pattern_subgraph.nodes()),
                    edges=list(pattern_subgraph.edges()),
                    metrics={
                        "node_count": pattern_subgraph.number_of_nodes(),
                        "edge_count": pattern_subgraph.number_of_edges()
                    },
                    query=query
                )
                extracted_subgraphs.append(subgraph_data)

        # --- Extract interdisciplinary (IBN) subgraph ---
        if graph.ibns:
            ibn_nodes = set(graph.ibns)

            for ibn_id in graph.ibns:
                if ibn_id in graph.graph:
                    ibn_nodes.update(graph.graph.predecessors(ibn_id))
                    ibn_nodes.update(graph.graph.successors(ibn_id))

            ibn_subgraph = graph.graph.subgraph(ibn_nodes)
            if ibn_subgraph.number_of_nodes() > 0:
                subgraph_data = self._build_subgraph_entry(
                    graph,
                    base_name="interdisciplinary",
                    base_desc="Subgraph highlighting interdisciplinary connections",
                    nodes=list(ibn_subgraph.nodes()),
                    edges=list(ibn_subgraph.edges()),
                    metrics={
                        "node_count": ibn_subgraph.number_of_nodes(),
                        "edge_count": ibn_subgraph.number_of_edges(),
                        "ibn_count": len(graph.ibns)
                    },
                    query=query
                )
                extracted_subgraphs.append(subgraph_data)

        logger.info(f"Subgraph extraction complete: Extracted {len(extracted_subgraphs)} subgraphs")

        return {
            "subgraphs": extracted_subgraphs,
            "summary": f"Extracted {len(extracted_subgraphs)} focused subgraphs for analysis",
            "metrics": {
                "subgraph_count": len(extracted_subgraphs)
            }
        }

    def _build_subgraph_entry(self, graph: AGoTGraph, base_name: str, base_desc: str,
                               nodes: List[str], edges: List[Tuple], metrics: Dict,
                               query: str) -> Dict[str, Any]:
        """构建子图条目，并用 AI 生成更语义化的名称/描述（失败时回退到硬编码默认值）"""
        entry = {
            "name": base_name,
            "description": base_desc,
            "nodes": nodes,
            "edges": edges,
            "metrics": metrics
        }
        label = self._ai_label_subgraph(graph, entry, query)
        if label:
            entry["name"] = label.name
            entry["description"] = label.description
        return entry

    def _ai_label_subgraph(self, graph: AGoTGraph, subgraph_data: Dict[str, Any],
                           query: str):
        """[AI] 为子图生成语义化名称和描述

        Returns SubgraphLabel on success, None on failure (fallback to defaults).
        """
        try:
            nodes = subgraph_data.get("nodes", [])[:10]  # Sample representative nodes
            node_samples = []
            for nid in nodes:
                if nid in graph.graph:
                    nd = graph.graph.nodes[nid]
                    label = nd.get("label", "")[:60]
                    node_samples.append(
                        f"- {nid}: label='{label}', type={nd.get('node_type', '')}, "
                        f"tags={nd.get('disciplinary_tags', [])}"
                    )

            prompt = HumanMessage(content=textwrap.dedent(f"""\
            Given a research query and an extracted subgraph from a knowledge graph, generate a CONCISE yet MEANINGFUL name and description for this subgraph.
            
            Research Query: "{query}"
            
            Representative nodes in this subgraph:
            {chr(10).join(node_samples) if node_samples else "(empty subgraph preview)"}
            
            Metrics: {subgraph_data.get('metrics', {})}
            
            Requirements:
            - name: short snake_case identifier capturing the subgraph's essence (e.g. "core_empirical_evidence")
            - description: one or two sentences explaining what this subgraph means for the research question
            """))
            result = simple_chat_model.with_structured_output(SubgraphLabel).invoke([prompt])
            if isinstance(result, SubgraphLabel) and result.name:
                return result
        except Exception as e:
            logger.warning(f"AI subgraph labeling failed, using default: {e}")
        return None
