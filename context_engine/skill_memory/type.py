from enum import Enum
from pydantic import BaseModel
from typing import Any, List, Optional, Literal

"""
    skill_memory 类型定义
    节点：TASK / SKILL / EVENT
    边：USED_SKILL / SOLVED_BY / REQUIRES / PATCHES / CONFLICTS_WITH
"""
# ─── 节点 ─────────────────────────────────────────────────────
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
    source_sessions: List[str]
    community_id: str | None
    pagerank: float
    created_at: int
    updated_at: int

# ─── 边 ───────────────────────────────────────────────────────

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
    condition: Optional[str]

class GmEdge(Edge):
    id: str
    session_id: str
    created_at: int

# ─── 信号 ─────────────────────────────────────────────────────
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

# ─── 提取结果 ─────────────────────────────────────────────────
class ExtractionResult(BaseModel):
    nodes: List[Node]
    edges: List[Edge]

class PromotedSkill(Node):
    type: Literal[NodeType.SKILL]

class FinalizeResult(BaseModel):
    promoted_skills: List[PromotedSkill]
    new_edges: List[Edge]
    invalidations: List[str]

# ─── 召回结果 ─────────────────────────────────────────────────
class RecallResult(BaseModel):
    nodes: List[GmNode]
    edges: List[GmEdge]
    token_estimate: int

# ─── 插件配置 ─────────────────────────────────────────────────
class GmConfig(BaseModel):
    db_path: str
    compact_turn_count: int
    recall_max_nodes: int
    recall_max_depth: int
    fresh_tail_count: int
    embedding: Optional[Any]
    llm: Optional[Any]
    dedup_threshold: float
    pagerank_damping: float
    pagerank_iterations: int