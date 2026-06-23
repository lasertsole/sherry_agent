from typing import Any


class Hyperedge:
    def __init__(self,
                 edge_id: str,
                 nodes: list[str],
                 confidence: float,
                 metadata: dict[str, Any] = None):
        self.edge_id = edge_id
        self.nodes = nodes
        self.confidence = confidence
        self.metadata = metadata or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "nodes": self.nodes,
            "confidence": self.confidence,
            **self.metadata
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'Hyperedge':
        edge_id = data.pop("edge_id")
        nodes = data.pop("nodes")
        confidence = data.pop("confidence")
        metadata = data
        return cls(edge_id, nodes, confidence, metadata)