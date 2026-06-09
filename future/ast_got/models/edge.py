import datetime
from typing import Dict, Any, Optional


class Edge:
    def __init__(self,
                 edge_id: str,
                 source: str,
                 target: str,
                 edge_type: str,
                 confidence: float,
                 metadata: Optional[Dict[str, Any]] = None):
        self.edge_id = edge_id
        self.source = source
        self.target = target
        self.edge_type = edge_type
        self.confidence = confidence
        self.metadata = metadata or {}

        if "timestamp" not in self.metadata:
            self.metadata["timestamp"] = str(datetime.datetime.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source": self.source,
            "target": self.target,
            "edge_type": self.edge_type,
            "confidence": self.confidence,
            **self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Edge':
        edge_id = data.pop("edge_id")
        source = data.pop("source")
        target = data.pop("target")
        edge_type = data.pop("edge_type")
        confidence = data.pop("confidence")
        metadata = data
        return cls(edge_id, source, target, edge_type, confidence, metadata)