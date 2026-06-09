import logging
import networkx as nx
from typing import Dict, Any, List, Optional, Set
import datetime

from tests import Node
from future.ast_got.models.edge import Edge
from future.ast_got.models.hyperedge import Hyperedge

logger = logging.getLogger("agot-graph")


class AGoTGraph:
    def __init__(self):
        self.graph = nx.DiGraph()
        self.hyperedges = {}
        self.layers = {}
        self.ibns = set()
        logger.info("AGoT Graph initialized")

    def add_node(self, node: Node) -> None:
        node_data = node.to_dict()
        self.graph.add_node(node.node_id, **node_data)

        if node.metadata.get("layer_id"):
            layer_id = node.metadata["layer_id"]
            if layer_id not in self.layers:
                self.layers[layer_id] = set()
            self.layers[layer_id].add(node.node_id)

        logger.debug(f"Added node {node.node_id} to graph")

    def add_edge(self, edge: Edge) -> None:
        edge_data = edge.to_dict()
        self.graph.add_edge(edge.source, edge.target, key=edge.edge_id, **edge_data)
        logger.debug(f"Added edge {edge.edge_id} to graph")

    def add_hyperedge(self, hyperedge: Hyperedge) -> None:
        self.hyperedges[hyperedge.edge_id] = hyperedge

        for i, source in enumerate(hyperedge.nodes):
            for target in hyperedge.nodes[i+1:]:
                virtual_edge_id = f"{hyperedge.edge_id}_virtual_{source}_{target}"

                self.graph.add_edge(
                    source, target,
                    key=virtual_edge_id,
                    edge_id=virtual_edge_id,
                    edge_type="hyperedge_virtual",
                    hyperedge_id=hyperedge.edge_id,
                    confidence=hyperedge.confidence,
                    is_virtual=True
                )

                self.graph.add_edge(
                    target, source,
                    key=virtual_edge_id + "_rev",
                    edge_id=virtual_edge_id + "_rev",
                    edge_type="hyperedge_virtual",
                    hyperedge_id=hyperedge.edge_id,
                    confidence=hyperedge.confidence,
                    is_virtual=True
                )

        logger.debug(f"Added hyperedge {hyperedge.edge_id} connecting {len(hyperedge.nodes)} nodes")

    def create_interdisciplinary_bridge(self, source_node_id: str, target_node_id: str) -> str:
        if source_node_id not in self.graph or target_node_id not in self.graph:
            raise ValueError("Both source and target nodes must exist in the graph")

        source_tags = set(self.graph.nodes[source_node_id].get("disciplinary_tags", []))
        target_tags = set(self.graph.nodes[target_node_id].get("disciplinary_tags", []))

        if source_tags.intersection(target_tags):
            logger.debug(f"Not creating IBN: nodes share disciplines {source_tags.intersection(target_tags)}")
            return None

        ibn_id = f"ibn_{source_node_id}_{target_node_id}"

        ibn_node = Node(
            node_id=ibn_id,
            label=f"IBN: {self.graph.nodes[source_node_id].get('label', 'Unknown')} <-> {self.graph.nodes[target_node_id].get('label', 'Unknown')}",
            node_type="interdisciplinary_bridge",
            confidence=[0.6, 0.6, 0.6, 0.6],
            metadata={
                "disciplinary_tags": list(source_tags.union(target_tags)),
                "source_disciplines": list(source_tags),
                "target_disciplines": list(target_tags),
                "provenance": "Interdisciplinary bridge creation",
                "creation_timestamp": str(datetime.datetime.now())
            }
        )

        self.add_node(ibn_node)

        source_edge = Edge(
            edge_id=f"{ibn_id}_source",
            source=source_node_id,
            target=ibn_id,
            edge_type="ibn_source",
            confidence=0.8,
            metadata={"provenance": "IBN connection"}
        )

        target_edge = Edge(
            edge_id=f"{ibn_id}_target",
            source=ibn_id,
            target=target_node_id,
            edge_type="ibn_target",
            confidence=0.8,
            metadata={"provenance": "IBN connection"}
        )

        self.add_edge(source_edge)
        self.add_edge(target_edge)

        self.ibns.add(ibn_id)

        logger.info(f"Created Interdisciplinary Bridge Node {ibn_id}")
        return ibn_id

    def update_node_confidence(self, node_id: str, new_confidence: List[float]) -> None:
        if node_id not in self.graph:
            raise ValueError(f"Node {node_id} not found in graph")

        self.graph.nodes[node_id]["confidence"] = new_confidence
        logger.debug(f"Updated confidence for node {node_id}: {new_confidence}")

    def update_edge_confidence(self, edge_id: str, new_confidence: float) -> None:
        for u, v, data in self.graph.edges(data=True):
            if data.get("edge_id") == edge_id:
                data["confidence"] = new_confidence
                logger.debug(f"Updated confidence for edge {edge_id}: {new_confidence}")
                return

        if edge_id in self.hyperedges:
            self.hyperedges[edge_id].confidence = new_confidence
            logger.debug(f"Updated confidence for hyperedge {edge_id}: {new_confidence}")
            return

        raise ValueError(f"Edge {edge_id} not found in graph")

    def calculate_topology_metrics(self) -> Dict[str, Dict[str, float]]:
        topology_metrics = {}

        degree_centrality = nx.degree_centrality(self.graph)
        betweenness_centrality = nx.betweenness_centrality(self.graph)
        closeness_centrality = nx.closeness_centrality(self.graph)
        clustering = nx.clustering(self.graph.to_undirected())

        for node_id in self.graph.nodes:
            topology_metrics[node_id] = {
                "degree_centrality": degree_centrality.get(node_id, 0),
                "betweenness_centrality": betweenness_centrality.get(node_id, 0),
                "closeness_centrality": closeness_centrality.get(node_id, 0),
                "clustering_coefficient": clustering.get(node_id, 0)
            }

            self.graph.nodes[node_id]["topology_metrics"] = topology_metrics[node_id]

        logger.info(f"Calculated topology metrics for {len(topology_metrics)} nodes")
        return topology_metrics

    def to_dict(self) -> Dict[str, Any]:
        nodes = []
        for node_id, data in self.graph.nodes(data=True):
            nodes.append({
                "node_id": node_id,
                "label": data.get("label", ""),
                "type": data.get("node_type", ""),
                "confidence": data.get("confidence", []),
                "metadata": {k: v for k, v in data.items() if k not in ["node_id", "label", "node_type", "confidence"]}
            })

        edges = []
        for u, v, data in self.graph.edges(data=True):
            if not data.get("is_virtual", False):
                edge_id = data.get("edge_id", f"{u}-{v}")
                edges.append({
                    "edge_id": edge_id,
                    "source": u,
                    "target": v,
                    "edge_type": data.get("edge_type", ""),
                    "confidence": data.get("confidence", 0.0),
                    "metadata": {key: val for key, val in data.items()
                                if key not in ["edge_id", "source", "target", "edge_type", "confidence", "is_virtual"]}
                })

        hyperedges = []
        for edge_id, hyperedge in self.hyperedges.items():
            hyperedges.append({
                "edge_id": edge_id,
                "nodes": hyperedge.nodes,
                "confidence": hyperedge.confidence,
                "metadata": hyperedge.metadata
            })

        layers_dict = {layer_id: list(nodes) for layer_id, nodes in self.layers.items()}

        return {
            "nodes": nodes,
            "edges": edges,
            "hyperedges": hyperedges,
            "layers": layers_dict,
            "metadata": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "hyperedge_count": len(hyperedges),
                "layer_count": len(layers_dict),
                "ibn_count": len(self.ibns)
            }
        }