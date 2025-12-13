from typing import Dict, Any, List


class Hyperedge:
    def __init__(self,
                 edge_id: str,
                 nodes: List[str],
                 confidence: float,
                 metadata: Dict[str, Any] = None):
        self.edge_id = edge_id
        self.nodes = nodes
        self.confidence = confidence
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "nodes": self.nodes,
            "confidence": self.confidence,
            **self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Hyperedge':
        edge_id = data.pop("edge_id")
        nodes = data.pop("nodes")
        confidence = data.pop("confidence")
        metadata = data
        return cls(edge_id, nodes, confidence, metadata)