from enum import Enum
from pydantic import BaseModel
from typing import Any, Literal

"""
    skill_memory type definitions
    Nodes: TASK / SKILL / EVENT
    Edges: USED_SKILL / SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH
"""
# ─── Nodes ────────────────────────────────────────────────────
class NodeType(Enum):
    TASK = "TASK"
    SKILL = "SKILL"
    EVENT = "EVENT"

class Node(BaseModel):
    type: NodeType
    name: str
    description: str
    content: str

class GmNode(Node):
    id: str
    validated_count: int
    source_sessions: list[str]
    community_id: str | None
    pagerank: float
    created_at: int
    updated_at: int

# ─── Edges ────────────────────────────────────────────────────

class EdgeType(Enum):
    USED_SKILL = "USED_SKILL"
    SOLVED_BY = "SOLVED_BY"
    REQUIRES = "REQUIRES"
    PATCHES = "PATCHES"
    CONFLICTS_WITH = "CONFLICTS_WITH"

class Edge(BaseModel):
    from_id: str
    to_id: str
    type: EdgeType
    instruction: str
    condition: str | None

class GmEdge(Edge):
    id: str
    session_id: str
    created_at: int

# ─── Signals ──────────────────────────────────────────────────
class SignalType(Enum):
    TOOL_ERROR = "tool_error"
    TOOL_SUCCESS = "tool_success"
    SKILL_INVOKED = "skill_invoked"
    USER_CORRECTION = "user_correction"
    EXPLICIT_RECORD = "explicit_record"
    TASK_COMPLETED = "task_completed"

class Signal(BaseModel):
    type: SignalType
    turn_index: int
    data: dict[str, Any]

# ─── Extraction Results ──────────────────────────────────────
class ExtractionResult(BaseModel):
    nodes: list[Node]
    edges: list[Edge]

class PromotedSkill(Node):
    type: Literal[NodeType.SKILL]

class FinalizeResult(BaseModel):
    promoted_skills: list[PromotedSkill]
    new_edges: list[Edge]
    invalidations: list[str]

# ─── Recall Results ──────────────────────────────────────────
class RecallResult(BaseModel):
    nodes: list[GmNode]
    edges: list[GmEdge]
    token_estimate: int

# ─── Plugin Config ───────────────────────────────────────────
class GmConfig(BaseModel):
    db_path: str
    compact_turn_count: int
    recall_max_nodes: int
    recall_max_depth: int
    fresh_tail_count: int
    embedding: Any | None
    llm: Any | None
    dedup_threshold: float
    pagerank_damping: float
    pagerank_iterations: int