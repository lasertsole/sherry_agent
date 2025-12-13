import datetime
from typing import List, Dict, Any, Optional


class Node:
    def __init__(self,
                 node_id: str,
                 label: str,
                 node_type: str,
                 confidence: List[float],
                 metadata: Optional[Dict[str, Any]] = None):
        self.node_id = node_id
        self.label = label
        self.node_type = node_type
        self.confidence = confidence
        self.metadata = metadata or {}

        if "timestamp" not in self.metadata:
            self.metadata["timestamp"] = str(datetime.datetime.now())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "node_type": self.node_type,
            "confidence": self.confidence,
            **self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Node':
        node_id = data.pop("node_id")
        label = data.pop("label")
        node_type = data.pop("node_type")
        confidence = data.pop("confidence")
        metadata = data
        return cls(node_id, label, node_type, confidence, metadata)